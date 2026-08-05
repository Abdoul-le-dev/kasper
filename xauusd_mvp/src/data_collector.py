"""
data_collector.py

Télécharge les ticks XAUUSD depuis Dukascopy sur la fenêtre définie dans
config.py, agrège en OHLC M1 en streaming (pour ne jamais garder tous les
ticks en mémoire), écrit les Parquet M1/M5/M15/M30/H1, sépare la quarantaine
(2 derniers mois) et produit un rapport de qualité.

Format Dukascopy .bi5 :
- URL : https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YEAR}/{MM-1:02d}/{DD:02d}/{HH:02d}h_ticks.bi5
  (attention : le MOIS est zero-indexé, janvier = 00)
- Contenu : LZMA compressed, N records de 20 bytes big-endian :
  uint32 ms_from_hour_start | uint32 ask_int | uint32 bid_int
  float32 ask_volume | float32 bid_volume
- Pour XAUUSD : ask_int / 1000 = ask price ; bid_int / 1000 = bid price

Ce module est idempotent : re-lancer skip ce qui existe déjà en local.

Usage :
    python -m src.data_collector [--force]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import lzma
import struct
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import httpx
import polars as pl

# import config : on tolère les deux modes d'exécution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("data_collector")


TICK_STRUCT = struct.Struct(">IIIff")  # 20 bytes par tick, big-endian
CACHE_DIR = config.DATA_DIR / "_dukascopy_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# URL & itération temporelle
# ---------------------------------------------------------------------------

def dukascopy_url(dt: datetime, instrument: str = config.DUKASCOPY_INSTRUMENT) -> str:
    """Construit l'URL Dukascopy pour une heure UTC donnée.

    ATTENTION : Dukascopy indexe les mois à partir de 0 (janvier = 00).
    """
    return (
        f"{config.DUKASCOPY_BASE_URL}/{instrument}/"
        f"{dt.year:04d}/{dt.month - 1:02d}/{dt.day:02d}/"
        f"{dt.hour:02d}h_ticks.bi5"
    )


def iter_hours(start_date: date, end_date: date) -> Iterator[datetime]:
    """Itère chaque heure UTC de start_date 00:00 à end_date 23:00 inclus.

    Skip les weekends (Dukascopy ne renvoie rien de significatif : forex fermé
    du vendredi 22h UTC au dimanche 22h UTC).
    """
    cur = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end = datetime.combine(end_date, time(23, 0), tzinfo=timezone.utc)
    while cur <= end:
        weekday = cur.weekday()  # 0=lun, 6=dim
        # forex fermé : samedi entier, dimanche jusqu'à 21h UTC, vendredi après 22h
        skip = False
        if weekday == 5:  # samedi
            skip = True
        elif weekday == 6 and cur.hour < 22:  # dimanche avant 22h
            skip = True
        elif weekday == 4 and cur.hour >= 22:  # vendredi après 22h
            skip = True
        if not skip:
            yield cur
        cur += timedelta(hours=1)


# ---------------------------------------------------------------------------
# Download + parsing bi5
# ---------------------------------------------------------------------------

def cache_path(dt: datetime) -> Path:
    return CACHE_DIR / f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}_{dt.hour:02d}.bi5"


def parse_bi5(raw: bytes, hour_start: datetime) -> List[Tuple[datetime, float, float, float, float]]:
    """Décompresse un blob .bi5 et retourne une liste de ticks.

    Retourne : [(timestamp_utc, bid, ask, bid_vol, ask_vol), ...]
    Un fichier vide ou non-décompressable → liste vide (pas d'exception).
    """
    if not raw:
        return []
    try:
        data = lzma.decompress(raw)
    except lzma.LZMAError:
        # Certaines heures sans ticks renvoient un blob non-LZMA. On ignore.
        return []

    ticks = []
    divisor = config.DUKASCOPY_PRICE_DIVISOR_XAUUSD
    for chunk in TICK_STRUCT.iter_unpack(data):
        ms, ask_int, bid_int, ask_vol, bid_vol = chunk
        ts = hour_start + timedelta(milliseconds=ms)
        ticks.append((ts, bid_int / divisor, ask_int / divisor, bid_vol, ask_vol))
    return ticks


async def fetch_hour(
    client: httpx.AsyncClient,
    dt: datetime,
    semaphore: asyncio.Semaphore,
) -> bytes:
    """Télécharge (ou lit du cache) le .bi5 d'une heure. Retourne bytes bruts."""
    cp = cache_path(dt)
    if cp.exists():
        return cp.read_bytes()

    url = dukascopy_url(dt)
    async with semaphore:
        for attempt in range(config.DUKASCOPY_RETRY_MAX):
            try:
                resp = await client.get(url, timeout=config.DUKASCOPY_TIMEOUT_S)
                if resp.status_code == 404:
                    # Heure sans données — on cache un blob vide pour éviter de retry
                    cp.write_bytes(b"")
                    return b""
                resp.raise_for_status()
                cp.write_bytes(resp.content)
                return resp.content
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                if attempt == config.DUKASCOPY_RETRY_MAX - 1:
                    logger.warning("Failed to fetch %s: %s", url, exc)
                    return b""
                await asyncio.sleep(2 ** attempt)
    return b""


# ---------------------------------------------------------------------------
# Aggregation M1 en streaming
# ---------------------------------------------------------------------------

@dataclass
class MinuteBar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    spread_sum: float  # somme des spreads pour calculer la moyenne
    spread_count: int


def ticks_to_m1(ticks: List[Tuple[datetime, float, float, float, float]]) -> List[MinuteBar]:
    """Agrège une liste de ticks en bougies M1. Utilise mid = (bid+ask)/2.

    Retourne une liste ordonnée de MinuteBar (peut couvrir plusieurs heures).
    """
    if not ticks:
        return []
    bars: dict = {}  # minute_ts -> MinuteBar
    for ts, bid, ask, _bv, _av in ticks:
        minute_ts = ts.replace(second=0, microsecond=0)
        mid = (bid + ask) / 2
        spread = ask - bid
        bar = bars.get(minute_ts)
        if bar is None:
            bars[minute_ts] = MinuteBar(
                ts=minute_ts,
                open=mid, high=mid, low=mid, close=mid,
                tick_volume=1,
                spread_sum=spread, spread_count=1,
            )
        else:
            if mid > bar.high:
                bar.high = mid
            if mid < bar.low:
                bar.low = mid
            bar.close = mid
            bar.tick_volume += 1
            bar.spread_sum += spread
            bar.spread_count += 1
    return [bars[k] for k in sorted(bars.keys())]


# ---------------------------------------------------------------------------
# Orchestration complète
# ---------------------------------------------------------------------------

async def collect_all_m1(start_date: date, end_date: date) -> pl.DataFrame:
    """Télécharge la plage, agrège en M1, retourne un DataFrame polars."""
    hours = list(iter_hours(start_date, end_date))
    total = len(hours)
    logger.info("Collecting %d hours (%s → %s)", total, start_date, end_date)

    semaphore = asyncio.Semaphore(config.DUKASCOPY_MAX_CONCURRENCY)
    all_bars: List[MinuteBar] = []

    async with httpx.AsyncClient(http2=False, follow_redirects=True) as client:
        # On traite par batches pour libérer la mémoire
        BATCH = 200
        for i in range(0, total, BATCH):
            batch = hours[i : i + BATCH]
            tasks = [fetch_hour(client, dt, semaphore) for dt in batch]
            blobs = await asyncio.gather(*tasks)
            for dt, blob in zip(batch, blobs):
                ticks = parse_bi5(blob, dt)
                all_bars.extend(ticks_to_m1(ticks))
            done = min(i + BATCH, total)
            pct = 100 * done / total
            logger.info("Progress: %d / %d hours (%.1f%%) — %d M1 bars collected",
                        done, total, pct, len(all_bars))

    if not all_bars:
        return pl.DataFrame()

    df = pl.DataFrame({
        "ts": [b.ts for b in all_bars],
        "open": [b.open for b in all_bars],
        "high": [b.high for b in all_bars],
        "low": [b.low for b in all_bars],
        "close": [b.close for b in all_bars],
        "tick_volume": [b.tick_volume for b in all_bars],
        "spread_avg": [b.spread_sum / b.spread_count for b in all_bars],
    }).sort("ts").unique(subset=["ts"], keep="last")

    return df


# ---------------------------------------------------------------------------
# Resampling M1 → M5/M15/M30/H1
# ---------------------------------------------------------------------------

def resample_m1(df_m1: pl.DataFrame, tf: str) -> pl.DataFrame:
    """Ré-échantillonne un DataFrame M1 vers un timeframe supérieur."""
    minutes = config.TIMEFRAME_MINUTES[tf]
    if minutes == 1:
        return df_m1
    return (
        df_m1
        .sort("ts")
        .group_by_dynamic("ts", every=f"{minutes}m", closed="left", label="left")
        .agg([
            pl.col("open").first().alias("open"),
            pl.col("high").max().alias("high"),
            pl.col("low").min().alias("low"),
            pl.col("close").last().alias("close"),
            pl.col("tick_volume").sum().alias("tick_volume"),
            pl.col("spread_avg").mean().alias("spread_avg"),
        ])
    )


# ---------------------------------------------------------------------------
# Rapport de qualité
# ---------------------------------------------------------------------------

def quality_report(df_m1: pl.DataFrame) -> dict:
    """Analyse la qualité des données M1 : gaps, cohérence OHLC, timezones."""
    if df_m1.is_empty():
        return {"empty": True}

    n = df_m1.height
    ts_min = df_m1["ts"].min()
    ts_max = df_m1["ts"].max()

    # Cohérence OHLC
    incoherent = df_m1.filter(
        (pl.col("high") < pl.col("low"))
        | (pl.col("high") < pl.col("open"))
        | (pl.col("high") < pl.col("close"))
        | (pl.col("low") > pl.col("open"))
        | (pl.col("low") > pl.col("close"))
    ).height

    # Gaps : différence entre timestamps consécutifs, hors weekends
    diffs = df_m1.select(
        (pl.col("ts").diff().dt.total_minutes()).alias("gap_min")
    ).drop_nulls()
    # gap "normal" = 1 minute. On flag les gaps > 5 min HORS weekend.
    # Un weekend fait ~48h × 60 = ~2880 min, on l'exclut.
    small_gaps = diffs.filter(pl.col("gap_min") > 5).filter(pl.col("gap_min") < 2000)
    big_gaps = diffs.filter(pl.col("gap_min") >= 2000)

    duplicates = n - df_m1.unique(subset=["ts"]).height

    return {
        "empty": False,
        "bars_count": n,
        "ts_min": str(ts_min),
        "ts_max": str(ts_max),
        "incoherent_ohlc": incoherent,
        "small_gaps_5_2000_min": small_gaps.height,
        "big_gaps_weekends": big_gaps.height,
        "duplicates_ts": duplicates,
        "spread_avg_mean": float(df_m1["spread_avg"].mean()),
        "spread_avg_p95": float(df_m1["spread_avg"].quantile(0.95)),
        "tick_volume_median": float(df_m1["tick_volume"].median()),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def split_and_save(df_m1: pl.DataFrame) -> dict:
    """Sépare quarantaine + hors quarantaine, écrit tous les Parquet."""
    quarantine_start = datetime.combine(
        config.QUARANTINE_START, time.min, tzinfo=timezone.utc
    )

    df_m1 = df_m1.with_columns(
        pl.col("ts").dt.replace_time_zone("UTC")
        if df_m1["ts"].dtype != pl.Datetime(time_zone="UTC")
        else pl.col("ts")
    ) if False else df_m1  # placeholder, tz gérée en amont

    train_m1 = df_m1.filter(pl.col("ts") < quarantine_start)
    quar_m1 = df_m1.filter(pl.col("ts") >= quarantine_start)

    results = {}
    for tf in config.TIMEFRAMES:
        train_df = resample_m1(train_m1, tf)
        quar_df = resample_m1(quar_m1, tf)

        train_path = config.DATA_DIR / f"xauusd_{tf.lower()}.parquet"
        quar_path = config.QUARANTINE_DIR / f"xauusd_{tf.lower()}.parquet"
        train_df.write_parquet(train_path)
        quar_df.write_parquet(quar_path)
        results[tf] = {
            "train_bars": train_df.height,
            "quarantine_bars": quar_df.height,
            "train_path": str(train_path),
            "quarantine_path": str(quar_path),
        }
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="Ignore le cache Dukascopy et re-télécharge tout")
    args = parser.parse_args()

    if args.force:
        for f in CACHE_DIR.glob("*.bi5"):
            f.unlink()
        logger.info("Cache Dukascopy purgé")

    logger.info("Config:\n%s", config.summary())

    df_m1 = asyncio.run(collect_all_m1(config.HISTORY_START, config.HISTORY_END))
    if df_m1.is_empty():
        logger.error("Aucune donnée récupérée. Aborting.")
        sys.exit(1)

    logger.info("Total M1 bars: %d", df_m1.height)

    report = quality_report(df_m1)
    logger.info("Quality report: %s", report)

    written = split_and_save(df_m1)
    logger.info("Parquet written:")
    for tf, info in written.items():
        logger.info("  %s : train=%d bars, quarantine=%d bars",
                    tf, info["train_bars"], info["quarantine_bars"])

    # Rapport écrit sur disque pour audit
    import json
    report_path = config.REPORTS_DIR / "data_quality.json"
    report_path.write_text(json.dumps(
        {"quality": report, "written": written}, indent=2, default=str
    ))
    logger.info("Rapport écrit : %s", report_path)


if __name__ == "__main__":
    main()
