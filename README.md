# 📸 Instagram → Telegram Photo Bot

Bot Telegram qui scrape les photos d’un profil Instagram et les envoie dans un topic dédié.

## Comment ça marche

1. Tu envoies un lien Instagram (`instagram.com/username`) au bot
1. Le bot scrape toutes les **photos** du profil via Apify (pas les vidéos, reels, stories)
1. Un **topic** est créé dans ton groupe Telegram avec le nom du profil
1. Toutes les photos sont envoyées dans ce topic

-----

## 🛠 Guide d’installation complet

### Étape 1 — Créer le bot Telegram

1. Ouvre Telegram et cherche **@BotFather**
1. Envoie `/newbot`
1. Donne un nom puis un username (ex: `insta_photo_dl_bot`)
1. **Copie le token** → tu en auras besoin

### Étape 2 — Préparer le groupe Telegram

1. Crée un **groupe** Telegram (ou utilise un existant)
1. Va dans **Paramètres du groupe → Topics** → **Active les topics**
1. **Ajoute ton bot** au groupe
1. **Rends le bot admin** avec les permissions :
- ✅ Gérer les topics
- ✅ Envoyer des messages
- ✅ Envoyer des photos/vidéos
1. Pour récupérer le **Group Chat ID** :
- Ajoute `@RawDataBot` au groupe
- Il envoie un message avec le `chat.id` (nombre négatif, ex: `-1001234567890`)
- **Retire `@RawDataBot`** du groupe ensuite

### Étape 3 — Créer un compte Apify

1. Va sur [apify.com](https://www.apify.com/) et crée un compte gratuit
1. Tu reçois **5$ de crédits gratuits** par mois
1. Va dans **Settings → Integrations → API Tokens**
1. Crée un token et **copie-le**

> ⚠️ Le scraper `apify/instagram-profile-scraper` consomme des crédits.
> Les 5$/mois gratuits suffisent pour ~10-15 profils moyens.

### Étape 4 — Push sur GitHub

```bash
# Clone ou crée le repo
git init instagram-telegram-bot
cd instagram-telegram-bot

# Copie tous les fichiers du projet ici, puis :
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/TON_USER/instagram-telegram-bot.git
git push -u origin main
```

### Étape 5 — Déployer sur Railway

1. Va sur [railway.app](https://railway.app/) et connecte-toi avec GitHub
1. Clique **“New Project”** → **“Deploy from GitHub Repo”**
1. Sélectionne ton repo `instagram-telegram-bot`
1. Railway va détecter le projet automatiquement

#### Configurer les variables d’environnement :

Dans Railway, va dans **ton service → Variables** et ajoute :

|Variable            |Valeur                                               |
|--------------------|-----------------------------------------------------|
|`TELEGRAM_BOT_TOKEN`|Le token de BotFather                                |
|`APIFY_API_TOKEN`   |Ton token API Apify                                  |
|`GROUP_CHAT_ID`     |L’ID du groupe (ex: `-1001234567890`)                |
|`ALLOWED_USER_IDS`  |*(optionnel)* Ton user ID Telegram, ex: `123456789`  |
|`MAX_POSTS`         |*(optionnel)* Limite de posts à scraper (défaut: 500)|


> Pour trouver ton user ID Telegram : envoie un message à `@userinfobot`

1. Railway déploie automatiquement. Vérifie les logs dans **Deployments → View Logs**

-----

## 💡 Utilisation

Envoie simplement au bot (en DM ou dans le groupe) :

```
https://instagram.com/natgeo
```

Le bot va :

- Scraper les photos de @natgeo
- Créer un topic `📸 @natgeo — 150 photos`
- Envoyer toutes les photos dans ce topic

### Commandes

- `/start` — Message de bienvenue
- `/help` — Aide
- `/info` — Config actuelle

-----

## ⚠️ Limitations

- **Profils privés** : impossible de scraper (Apify ne peut pas y accéder)
- **Rate limits Telegram** : le bot envoie par lots de 10 avec des pauses
- **Crédits Apify** : 5$/mois gratuits, au-delà c’est payant
- **Topics Telegram** : le groupe DOIT avoir les topics activés