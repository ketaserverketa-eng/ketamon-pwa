Security notes — KetaMon

Résumé des changements importants:
- `KETAMON_SECRET_KEY` : configurez en production pour sessions persistantes.
- `KETAMON_ROUTERS_KEY` (optionnel) : si défini (Fernet key), les mots de passe des routeurs stockés dans `data/ketamon.db` sont chiffrés au repos.
- Les anciens `data/users.json` et `data/routers.json` sont migrés automatiquement vers SQLite au démarrage si les tables locales sont vides.
- CSRF : token simple injecté dans les templates et vérifié pour toutes les requêtes POST. Assurez-vous d'inclure `{{ csrf_token }}` dans vos formulaires si vous avez des templates personnalisés.
- Uploads : taille limitée à 5 MB et extensions logos restreintes (.png, .jpg, .jpeg, .gif, .svg).
- `demarrer.bat` : évite désormais de tuer tous les `python.exe` — il cible uniquement les processus qui lancent `app.py`.

Recommandations opérationnelles:
1. Définir `KETAMON_SECRET_KEY` dans l'environnement avant déploiement.
2. Si vous stockez mots de passe routeur, fournissez `KETAMON_ROUTERS_KEY` avant le premier démarrage ou avant la migration des données legacy.
3. Restreindre accès réseau aux routeurs (firewall), préférer tunnels chiffrés/SSH pour management.
4. Exécuter derrière un reverse-proxy TLS (nginx, Caddy) et ne pas exposer l'API admin sans authentification forte.
5. Sauvegarder `data/ketamon.db` régulièrement ; les JSON legacy ne sont plus la source de vérité après migration.

Procédure de migration chiffrement (résumé):
- Installer dépendances: `pip install -r requirements.txt` (voir `cryptography`)
- Exporter clé: `set KETAMON_ROUTERS_KEY=<FERNET_KEY>` (Windows) ou `export KETAMON_ROUTERS_KEY=...`
- Les nouveaux enregistrements SQLite seront chiffrés automatiquement.
- `python scripts/encrypt_routers.py` reste disponible uniquement pour préparer un ancien `routers.json` avant migration.

Notes de sécurité supplémentaires:
- Changez le mot de passe initial `admin` imprimé au premier démarrage.
- Le fallback concepteur local n'accepte plus de mot de passe en clair ; utilisez un hash Werkzeug (`pbkdf2:` ou `scrypt:`) dans `concepteur.json`.
- Ne stockez pas `KETAMON_SECRET_KEY` ou `KETAMON_ROUTERS_KEY` dans le dépôt.
- Audit: effectuez des tests de charge et de sécurité avant production (scans CVE, tests d'injection, revue des templates).

- En mode production (`KETAMON_ENV=production`), `KETAMON_SECRET_KEY` est d?sormais obligatoire au d?marrage.
