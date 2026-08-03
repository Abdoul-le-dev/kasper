"""
get_telegram_chat_id.py

Utilitaire à usage unique pour récupérer le chat_id Telegram manquant.

MARCHE À SUIVRE :
1. Ouvre Telegram, cherche ton bot par son @username (celui donné par @BotFather).
2. Envoie-lui n'importe quel message (ex: "salut").
3. Lance ce script avec ton TELEGRAM_BOT_TOKEN :

    python3 get_telegram_chat_id.py <TON_BOT_TOKEN>

4. Le script affichera le chat_id à utiliser dans TELEGRAM_CHAT_ID.

Si tu veux notifier un GROUPE plutôt qu'un chat privé :
- Ajoute le bot au groupe
- Envoie un message dans le groupe (en mentionnant le bot ou non)
- Relance ce script : le chat_id du groupe apparaîtra (souvent négatif, ex: -1001234567890)
"""

import sys
import httpx


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 get_telegram_chat_id.py <TELEGRAM_BOT_TOKEN>")
        sys.exit(1)

    token = sys.argv[1]
    url = f"https://api.telegram.org/bot{token}/getUpdates"

    try:
        response = httpx.get(url, timeout=10.0)
    except httpx.HTTPError as exc:
        print(f"Erreur réseau: {exc}")
        sys.exit(1)

    if response.status_code != 200:
        print(f"Erreur API Telegram ({response.status_code}): {response.text}")
        sys.exit(1)

    data = response.json()
    results = data.get("result", [])

    if not results:
        print(
            "Aucun message reçu par le bot pour l'instant.\n"
            "→ Envoie d'abord un message au bot (ou ajoute-le à ton groupe et écris un message), "
            "puis relance ce script."
        )
        sys.exit(0)

    seen = set()
    print("Chats détectés :\n")
    for update in results:
        message = update.get("message") or update.get("channel_post")
        if not message:
            continue
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        chat_type = chat.get("type")
        chat_title = chat.get("title") or chat.get("username") or chat.get("first_name")
        if chat_id in seen:
            continue
        seen.add(chat_id)
        print(f"  chat_id = {chat_id}   (type: {chat_type}, nom: {chat_title})")

    print("\nCopie le chat_id correspondant dans la variable d'environnement TELEGRAM_CHAT_ID.")


if __name__ == "__main__":
    main()
