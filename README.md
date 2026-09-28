# Insta Save Engine

Transforme tes posts Instagram sauvegardés en une vraie base d'idées de contenu, rangée et exploitable, dans Notion. En local, sur ton Mac, gratuitement.

Tu sauvegardes des posts sur Instagram toute la journée (des prompts, des outils, des repos, des idées de vidéo) et ils dorment dans un dossier que tu ne rouvres jamais. Cet outil va les chercher, crée des catégories adaptées à ce que tu sauvegardes, lit le contenu qui est dans l'image ou dans l'audio du reel (pas juste la légende), en extrait l'essentiel et écrit tout ça dans Notion. Tu te retrouves avec une bibliothèque cherchable au lieu d'un cimetière de saves.

Fait par [vousyetes](https://instagram.com/vousyetes). Licence MIT, tu en fais ce que tu veux.

---

## Ce que ça fait, concrètement

1. **Récupère** tes posts sauvegardés Instagram (tout, ou seulement certaines collections).
2. **Classe** chaque post dans des catégories taillées pour TON contenu. Au premier passage, l'IA locale lit un échantillon de tes saves et génère 6 à 10 catégories qui collent à ce que tu sauvegardes (cuisine, code, déco, peu importe), puis range chaque post dedans. Sans Ollama (mode léger), elle retombe sur un jeu de catégories génériques par mots-clés.
3. **Lit le média** quand la valeur n'est pas dans la légende : OCR des slides d'un carrousel, transcription de l'audio d'un reel, texte à l'écran. Tout en local, aucune donnée qui part ailleurs.
4. **Extrait** le contenu utile : le prompt copiable, le nom de l'outil et son lien, les étapes d'un workflow, l'astuce en une phrase.
5. **Écrit** le tout dans deux bases Notion : une pour les saves bruts, une pour les idées de contenu prêtes à produire.
6. **Récupère les liens partagés depuis l'iPhone** sans ouvrir de nouvelle session Instagram.
7. **Déduit les thèmes récurrents** dans ta catégorie fourre-tout avec Ollama, sans modifier Notion.
8. **Construit un index hors ligne** en JSONL, Markdown et HTML, avec une recherche locale en ligne de commande.

Tout tourne sur ta machine. Les modèles IA sont locaux (Ollama + whisper). Ça ne coûte rien à faire tourner, et tes saves ne quittent jamais ton Mac.

---

## Deux façons de l'installer

**Mode complet** : tu installes les modèles IA locaux (environ 26 Go). L'outil lit vraiment le contenu des images et des vidéos. C'est là que la magie opère.

**Mode léger** : pas de modèles, pas de lecture du média. Tu récupères juste tes saves et leur classement automatique. C'est déjà très utile, ça pèse presque rien, et tu pourras passer en mode complet plus tard sans rien casser.

Le README couvre les deux. Choisis en fonction de la place que tu as et de si la lecture profonde des reels t'intéresse.

---

## Prérequis

- **Un Mac** (Apple Silicon M1/M2/M3/M4 ou Intel). Tout est optimisé et testé sur Apple Silicon.
- **Python 3.11 ou plus récent** (3.13 recommandé).
- **Homebrew** ([brew.sh](https://brew.sh)) pour installer les quelques outils système.
- **Un compte Notion** (le plan gratuit suffit).
- **Un compte Instagram** avec des posts sauvegardés.
- Pour le mode complet uniquement : environ **30 Go de libre** sur le disque (les modèles IA), et un Mac avec au moins 16 Go de RAM confortable.

Si tu n'as pas encore Homebrew et Python :

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install python@3.13
```

---

## Installation rapide (le script fait le gros du travail)

Récupère le projet et lance le script d'install. Il crée l'environnement Python, installe les dépendances, télécharge les modèles IA, récupère un modèle whisper et prépare tout le reste.

```bash
git clone https://github.com/vousyetes/insta-save-engine.git
cd insta-save-engine
./install.sh
```

Pour le **mode léger** (sans les 26 Go de modèles) :

```bash
./install.sh --light
```

Le script est bavard : il te dit à chaque étape ce qu'il fait et ce qui reste à faire à la main. Une fois qu'il a fini, il te reste trois choses à régler : Notion, Instagram, et le premier run. C'est la suite.

---

## Étape 1 : configurer Notion (en grande partie automatique)

L'outil écrit dans deux bases Notion. Bonne nouvelle : tu n'as pas à les construire à la main, un script les crée pour toi avec les bons champs. Il te faut juste deux choses au départ : un jeton d'intégration, et une page où poser les bases.

### 1a. Crée une intégration Notion et récupère son jeton

1. Va sur [notion.so/my-integrations](https://www.notion.so/my-integrations).
2. Clique **New integration**, donne-lui un nom (par exemple `Insta Save Engine`), valide.
3. Copie le **Internal Integration Token** (il commence par `ntn_`).

Colle ce jeton dans `config.json`, à la place de `ntn_PASTE_YOUR_NOTION_INTEGRATION_TOKEN_HERE` :

```bash
open -e config.json
```

### 1b. Crée une page Notion et partage-la avec l'intégration

1. Dans Notion, crée une page vide (appelle-la comme tu veux, par exemple `Insta Save Engine`).
2. En haut à droite de la page, ouvre le menu **•••** puis **Connections** (ou **Connexions**), et ajoute ton intégration. C'est cette étape qui autorise le script à écrire dans la page. Si tu la sautes, rien ne marchera.
3. Copie l'URL de la page (le bouton **Share** puis **Copy link**, ou l'URL dans ton navigateur).

Colle cette URL dans `config.json`, à la place de `PASTE_THE_ID_OF_THE_NOTION_PAGE...`. Tu peux coller l'URL complète, le script sait retrouver l'identifiant tout seul.

### 1c. Lance la création des bases

```bash
.venv/bin/python setup_notion.py
```

Le script crée les deux bases (`Instagram Saves` et `Content Ideas`) dans ta page, avec tous les bons champs, et écrit leurs identifiants dans `config.json` à ta place. Si ça râle, c'est presque toujours que la page n'est pas partagée avec l'intégration (reviens au point 1b).

---

## Étape 2 : se connecter à Instagram (une seule fois)

L'outil se connecte à ton compte avec ton identifiant et ton mot de passe, stockés dans le **Trousseau macOS** (jamais dans un fichier, jamais en clair). Il garde ensuite une session et se reconnecte tout seul quand elle expire. Tu ne touches plus jamais à un cookie.

```bash
.venv/bin/python setup_auth.py
```

On te demande ton identifiant, ton mot de passe (masqué), et éventuellement le code 2FA. Si tu utilises une app d'authentification (TOTP), colle sa clé secrète pour que les reconnexions restent automatiques. Au premier run, le Trousseau peut demander une autorisation : choisis **Toujours autoriser** pour que la tâche planifiée tourne sans te déranger.

Tu avais déjà configuré l'outil sans TOTP ? Active d'abord l'application d'authentification dans Instagram, choisis l'option de configuration manuelle, puis stocke la clé sans relancer de connexion :

```bash
.venv/bin/python set_totp.py
```

> Note : Instagram n'aime pas les connexions à répétition. L'outil est fait pour se connecter le moins possible (il réutilise sa session au lieu de se relogger à chaque fois). Ne relance pas `setup_auth.py` en boucle « pour tester », une fois suffit.

---

## Étape 3 : premier run

```bash
.venv/bin/python sync.py       # récupère tes saves dans Notion
.venv/bin/python ideate.py     # les classe en idées de contenu
.venv/bin/python extract.py --enrich --limit 100   # lecture IA du média + extraction
```

Ouvre ta page Notion : tes deux bases se remplissent. La première fois, `sync.py` peut ramener plusieurs centaines de posts, c'est normal.

Au tout premier `ideate.py`, l'outil s'arrête une minute pour lire un échantillon de tes saves et générer les catégories qui te correspondent (il faut qu'Ollama tourne). Elles sont écrites dans `config.json`, tu peux les relire ou les retoucher à la main :

```bash
.venv/bin/python discover_categories.py --show     # voir tes catégories
.venv/bin/python discover_categories.py --force     # les régénérer de zéro
```

En **mode léger**, tu t'arrêtes après `ideate.py` (pas de `extract.py`, il a besoin d'Ollama). Sans Ollama, la génération de catégories ne tourne pas non plus : le classement retombe sur un jeu générique par mots-clés.

---

## Automatiser (optionnel) : sync deux fois par jour

Le script d'install génère un fichier `com.user.insta-save-engine.plist` avec tes chemins déjà remplis. Pour que macOS lance le sync automatiquement à 9h et 21h :

```bash
cp com.user.insta-save-engine.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.user.insta-save-engine.plist
```

Pour le désactiver plus tard :

```bash
launchctl unload ~/Library/LaunchAgents/com.user.insta-save-engine.plist
```

(La tâche ne lance que `sync.py`. Le classement et l'extraction, tu les lances quand tu veux, ou tu les ajoutes toi-même à ta routine.)

---

## Interroger ta base

Une fois que ça tourne, `query.py` te sort ce que tu cherches sans ouvrir Notion :

```bash
.venv/bin/python query.py                 # résumé par catégorie
.venv/bin/python query.py REPO            # tous les repos sauvegardés
.venv/bin/python query.py PROMPT claude   # les prompts qui parlent de "claude"
.venv/bin/python query.py OUTIL           # tous les outils
```

Par défaut, les « teasers » (les posts qui ne donnent le contenu qu'en DM après commentaire) sont masqués. Ajoute `--teasers` pour les voir quand même.

Si tu utilises Claude Code, deux commandes sont fournies dans `.claude/commands/` : `/sync-instagram` (lance le pipeline) et `/ideas` (interroge ta base).

---

## Partager un post depuis l'iPhone, sans session Instagram

`inbox.py` lit un simple fichier texte synchronisé par iCloud Drive. Chaque ligne peut contenir un lien Instagram ou TikTok. Le script utilise les métadonnées publiques récupérées par `yt-dlp`, vérifie les doublons dans Notion, crée les nouvelles fiches, puis retire du fichier les lignes traitées. Une ligne en échec reste dans l'inbox pour le prochain passage.

1. Sur l'iPhone, crée un raccourci disponible dans la feuille de partage.
2. Fais-lui ajouter l'URL reçue à un fichier texte dans iCloud Drive, une URL par ligne.
3. Sur le Mac, retrouve ce fichier dans le Finder, maintiens Option, fais un clic droit, puis choisis **Copier comme nom de chemin**.
4. Colle ce chemin dans `config.json` sous `iphone_inbox_path`.

Teste d'abord sans rien écrire :

```bash
.venv/bin/python inbox.py --dry-run
```

Puis importe réellement les liens :

```bash
.venv/bin/python inbox.py
```

Tu peux aussi fournir un fichier ponctuel avec `--file /chemin/vers/inbox.txt`. Cette fonction a besoin de `yt-dlp`, installé par `install.sh`. En installation manuelle : `brew install yt-dlp`.

---

## Trouver les thèmes cachés dans le fourre-tout

`themes.py` lit les fiches dont le titre commence par la catégorie configurée dans `catch_all_category` (par défaut `INSPIRATION`). Ollama attribue un ou deux thèmes courts à chaque fiche, réutilise les thèmes existants quand ils conviennent, puis écrit deux fichiers locaux ignorés par git :

- `themes-cache.json`, utilisé par l'index et par les passages suivants.
- `themes-proposed.md`, rapport lisible avec les volumes et quelques exemples.

Le script ne modifie jamais Notion.

```bash
.venv/bin/python themes.py
.venv/bin/python themes.py --category INSPIRATION --min 5
.venv/bin/python themes.py --refresh --limit 40
```

Il faut le modèle texte configuré dans `text_model` et un serveur Ollama actif.

---

## Construire et chercher l'index hors ligne

`index.py` lit les deux bases Notion, rapproche chaque idée de sa source, ajoute les thèmes du cache, puis produit :

- `index/index.jsonl`, la source compacte utilisée par la recherche.
- `index/index.md`, un index lisible partout.
- `index/vault.html`, une interface autonome avec recherche et filtre par catégorie.

```bash
.venv/bin/python index.py
open index/vault.html
.venv/bin/python demande.py "workflow vidéo"
.venv/bin/python demande.py "recette rapide" --limit 10
```

`demande.py` ne fait aucun appel réseau et n'utilise aucun modèle. Si tu veux une copie du Markdown et du HTML dans un autre dossier synchronisé, indique son chemin dans `offline_index_copy_dir`. Laisse le champ vide pour désactiver la copie.

---

## Installation manuelle (si tu préfères tout faire à la main)

Le script `install.sh` fait exactement ça, mais si tu veux comprendre ou contrôler chaque étape :

```bash
# 1. Environnement Python
python3.13 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 2. Fichier de config
cp config.example.json config.json
chmod 600 config.json
# (remplis config.json : jeton Notion + id de la page parente)

# 3. Outil de partage iPhone
brew install yt-dlp

# 4. Outils système (mode complet seulement)
brew install ffmpeg whisper-cpp
brew install ollama            # ou télécharge l'app sur ollama.com

# 5. Modèles IA locaux (mode complet seulement, ~26 Go)
ollama pull qwen2.5vl:7b       # vision : lit le texte dans les images
ollama pull gpt-oss:20b        # texte : extrait l'essentiel

# 6. Modèle whisper pour transcrire l'audio des reels (~550 Mo)
mkdir -p models
curl -L -o models/ggml-large-v3-turbo-q5_0.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin
# puis mets ce chemin dans config.json → "whisper_model_path"
```

Le champ `whisper_model_path` dans `config.json` pointe vers ton modèle whisper. S'il est vide ou introuvable, les reels sont quand même lus (OCR du texte à l'écran), il manque juste la transcription de l'audio parlé.

---

## À quoi ressemble config.json

```json
{
  "notion_token": "ntn_...",                    // ton jeton d'intégration
  "notion_parent_page_id": "...",               // la page qui accueille les 2 bases
  "instagram_saves_db_id": "",                  // rempli par setup_notion.py
  "content_ideas_db_id": "",                    // rempli par setup_notion.py
  "iphone_inbox_path": "",                      // fichier texte iCloud alimenté par le raccourci iPhone
  "catch_all_category": "INSPIRATION",          // catégorie analysée par themes.py
  "offline_index_copy_dir": "",                 // copie facultative de l'index HTML et Markdown
  "instagram_collections": [],                  // [] = toutes tes collections ; sinon ["AI", "Inspiration"]
  "categories": [],                              // rempli au 1er run par l'IA (catégories taillées pour toi)
  "instagram_expected_user_id": "",             // laisse vide (garde-fou multi-comptes, optionnel)
  "login_country": "",                           // optionnel, ex "FR" (voir plus bas)
  "login_country_code": "",                      // optionnel, ex 33
  "login_locale": "",                            // optionnel, ex "fr_FR"
  "login_tz_offset": "",                         // optionnel, décalage en secondes, ex 3600
  "whisper_model_path": "",                      // rempli par install.sh
  "vision_model": "qwen2.5vl:7b",
  "text_model": "gpt-oss:20b",
  "instagram_cookies": { "sessionid": "", "csrftoken": "", "ds_user_id": "" }
}
```

`instagram_cookies` sert seulement de secours si `setup_auth.py` n'est pas encore passé. En temps normal, tu le laisses vide, la connexion se fait toute seule via le Trousseau.

Les quatre champs `login_*` sont optionnels et vides par défaut. Instagram fait moins de contrôles quand la région déclarée à la connexion correspond à celle du compte. Si tu te fais embêter par des demandes de vérification, renseigne ton pays. Pour la France : `"login_country": "FR"`, `"login_country_code": 33`, `"login_locale": "fr_FR"`, `"login_tz_offset": 3600` (mets 7200 en heure d'été).

---

## Dépannage

**« 403 » ou session refusée pendant le sync.**
Le plus souvent c'est un contrôle temporaire d'Instagram, pas une vraie panne. Attends quelques heures et relance `sync.py` : dans la majorité des cas, ça repasse tout seul. Si Instagram t'envoie une alerte « c'était toi ? » dans l'app, confirme que oui (depuis ton wifi habituel), ça débloque la situation plus vite. Ne relance pas le sync dix fois d'affilée, chaque tentative de connexion nourrit la suspicion.

**« Login cooldown active » dans le log.**
Ce n'est pas une panne, c'est le garde-fou. Après une vraie connexion par mot de passe, l'outil s'interdit d'en renvoyer une pendant 20 h, parce que chaque connexion déclenche une alerte de sécurité et pousse le compte vers un checkpoint. Le message te dit quand ça se lève, et le run suivant repart tout seul. **Ne supprime pas `.last_login` pour forcer** : c'est exactement l'enchaînement de connexions qui fait bloquer un compte.

**« Instagram raised a challenge ».**
Instagram veut que tu approuves la connexion depuis l'app Instagram sur ton téléphone. Personne ne peut le faire à ta place. Approuve-la, attends le cooldown, et laisse le run suivant repartir seul. Le moteur ne retente pas un vieux cookie navigateur après cet échec et coupe un login bloqué après deux minutes.

**« Ollama non disponible » pendant l'extraction.**
Le serveur Ollama ne tourne pas. Ouvre l'app Ollama, ou lance `ollama serve` dans un terminal, puis relance `extract.py`. En attendant, `sync.py` et `ideate.py` marchent très bien sans lui.

**L'extraction renvoie « vide » sur des posts qui ont pourtant du contenu.**
Souvent normal : beaucoup de posts « prompt » sur Instagram sont des appâts qui n'envoient le vrai contenu qu'en DM. L'outil les repère et pose un `⏳` au lieu d'inventer. Si ça arrive sur tout, vérifie que le modèle `gpt-oss:20b` est bien téléchargé (`ollama list`).

**Notion refuse de créer les bases.**
La page parente n'est pas partagée avec l'intégration. Ouvre la page → menu **•••** → **Connections** → ajoute ton intégration, puis relance `setup_notion.py`.

**Le sync ne ramène aucun post.**
Vérifie que `setup_auth.py` est bien passé (connexion OK) et que tu as des posts sauvegardés sur le compte connecté. Regarde `sync.log` pour le détail.

**whisper-cli introuvable.**
`brew install whisper-cpp`. Sans lui, les reels sont lus en OCR uniquement (le texte à l'écran), ce qui suffit souvent.

---

## Comment ça marche (et pourquoi c'est fait comme ça)

Le pipeline est volontairement découpé en scripts indépendants, pour que chaque étape soit relançable seule et que rien ne casse tout le reste en cas de souci.

- **`sync.py`** : va chercher tes saves via l'API privée d'Instagram, récupère la miniature (elle devient la cover de la page Notion) et écrit chaque post dans la base `Instagram Saves` avec le statut `New`. Il déduplique via `state.json`, donc tu peux le relancer sans créer de doublons.
- **`auth.py` / `setup_auth.py` / `set_totp.py`** : la connexion Instagram. Le choix ici est « stable et discret ». On stocke les identifiants et la graine TOTP dans le Trousseau, on garde une session sur disque, et surtout on évite de se reconnecter à chaque run. La sonde de session utilise un endpoint doux, pas le fil principal qui est sur-surveillé. Un disjoncteur (`.last_login`) impose 20 h entre deux connexions par mot de passe, même si la session est morte ; tout login bloqué est coupé après deux minutes.
- **Un seul appareil, du login à la lecture.** `sync.py` lit via l'API mobile signée d'instagrapi, celle-là même qui a créé la session. C'est important : rejouer les cookies d'une session mobile derrière un User-Agent de navigateur fait croire à Instagram qu'un même compte est utilisé depuis un téléphone et depuis un ordinateur en alternance, donc qu'on lui a volé sa session. Il tue la session, le run suivant doit se reconnecter, et cette connexion attire un challenge. Symptôme : un sync qui marche un jour ou deux, casse, puis remarche. Le cookie navigateur n’est admis qu’avant la première configuration du Trousseau ; il ne sert jamais de repli après un échec de l’authentification gérée.
- **`discover_categories.py`** : au premier passage, lit un échantillon de tes saves et demande au modèle local de dessiner un jeu de catégories qui colle à TON contenu (nom, description, pilier). Écrit dans `config.json`. C'est ce qui rend l'outil universel au lieu d'être coincé sur une seule niche.
- **`ideate.py`** : le classement. Quand tes catégories existent et qu'Ollama tourne, l'IA locale range chaque post dans la bonne (elle lit la caption, pas juste des mots-clés). Sinon, filet de secours par règles (mots-clés) avec un jeu générique. Chaque save `New` devient une idée dans `Content Ideas`, taguée par catégorie et rangée dans un pilier.
- **`enrich.py`** : la lecture du média. Quand la valeur est dans l'image ou la vidéo, on télécharge le média et on le fait lire par les modèles locaux. La résolution vision est montée à 1536px parce qu'en dessous, le petit texte se coupe et le modèle se met à inventer des noms.
- **`extract.py`** : l'extraction finale. Il envoie la légende (plus le texte lu dans le média si `--enrich`) au modèle texte local, avec un prompt spécifique à chaque catégorie, et écrit le résultat propre dans Notion. Il garde aussi le texte brut lu dans le média dans un bloc repliable, au cas où tu veuilles y revenir.
- **`query.py`** : la recherche, pour piocher dans ta base depuis le terminal.
- **`inbox.py`** : importe les liens partagés depuis un fichier iCloud sans utiliser la session Instagram du moteur.
- **`themes.py`** : repère localement les thèmes qui se répètent dans la catégorie fourre-tout et produit un rapport sans écrire dans Notion.
- **`index.py` / `demande.py`** : construisent une archive autonome et permettent de l'interroger hors ligne.

Le fil rouge : tout est local, tout est gratuit à faire tourner, et chaque brique est idempotente (tu peux relancer sans tout casser).

---

## Sécurité et vie privée

- Aucun secret n'est dans ce dépôt. `config.json`, la session Instagram et les logs sont ignorés par git (voir `.gitignore`).
- Tes identifiants Instagram vivent dans le Trousseau macOS, jamais dans un fichier.
- Tes saves et le contenu lu ne quittent jamais ta machine. Les modèles IA tournent en local. Rien n'est envoyé à un service externe, à part l'écriture dans ta propre base Notion.

---

## Licence

MIT. Utilise-le, modifie-le, partage-le. Si ça te sert, un crédit à [vousyetes](https://instagram.com/vousyetes) fait toujours plaisir.
