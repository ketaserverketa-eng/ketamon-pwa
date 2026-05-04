KetaMon - Plateforme SaaS WiFi (tickets hotspot)

KetaMon est maintenant oriente SaaS:
- chaque utilisateur gere son propre business WiFi;
- chaque tenant cree ses tickets hotspot, suit ses revenus et connecte ses routeurs;
- la plateforme gere l'authentification, l'isolation multi-tenant, la securite, la supervision technique et la migration legacy.

Runtime officiel
- `D:\ketaserver\ketamon\demarrer.bat` lance le backend FastAPI/Mongo officiel sur `http://localhost:5001` par defaut.
- `D:\ketaserver\ketamon\demarrer_legacy_flask.bat` lance le Flask legacy sur `http://localhost:5002` par defaut.
- Le mode legacy reste transitoire: il ne doit plus modifier les tickets, revenus ou routeurs cloud des clients.

Architecture
- API officielle: `D:\ketaserver\ketamon\backend\app.py`
- Adaptateurs routeurs: `D:\ketaserver\ketamon\backend\router_adapters.py`
- Legacy Flask: `D:\ketaserver\ketamon\app.py`
- SQLite legacy local: `D:\ketaserver\ketamon\database.py`
- Ops: `D:\ketaserver\ketamon\backend\Dockerfile`, `D:\ketaserver\ketamon\backend\docker-compose.yml`, `D:\ketaserver\ketamon\backend\nginx.conf`

Modele SaaS
- Collections Mongo: `tenants`, `users`, `routers`, `tickets`, `revenue_events`, `audit_logs`
- Schema canonique tickets:
  - `_id`, `tenant_id`, `owner_id`, `code`, `prix`, `devise`
  - `date_creation`, `date_premiere_utilisation`, `date_expiration`, `statut`
  - `router_id`, `device_policy`, `router_policy`
  - `premier_appareil_id`, `premier_routeur_id`
  - `date_revocation`, `date_derniere_utilisation`
- Schema canonique revenus:
  - `_id`, `tenant_id`, `owner_id`, `ticket_id`, `code`, `prix`, `devise`
  - `date_premiere_utilisation`, `router_id`, `appareil_id`

Fonctionnalites principales
- JWT pour `register`, `login`, `refresh`
- Tickets hotspot limites dans le temps
- Historique tickets par tenant
- Revenus par aggregation MongoDB
- Compatibilite MikroTik avec detection RouterOS et strategie `v6` / `v7` / compatibilite future
- Rate limiting avec Redis si disponible, sinon memoire en developpement
- Migration automatique des anciennes donnees Flask au demarrage

Demarrage local
1. Installer Python 3.11+ et MongoDB.
2. Creer l'environnement backend puis installer les dependances:
   ```powershell
   cd D:\ketaserver\ketamon\backend
   python -m venv .venv
   .\.venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Copier `D:\ketaserver\ketamon\backend\.env.example` vers `.env` puis definir au minimum:
   ```powershell
   $env:MONGO_URL="mongodb://localhost:27017"
   $env:MONGO_DB="ketamon"
   $env:JWT_SECRET="remplace_par_un_secret_solide"
   $env:JWT_REFRESH_SECRET="remplace_par_un_autre_secret_solide"
   ```
4. Lancer l'API officielle:
   ```powershell
   D:\ketaserver\ketamon\demarrer.bat
   ```

Stack Docker locale
1. Depuis `D:\ketaserver\ketamon\backend`, configurer `.env`.
2. Lancer la stack:
   ```powershell
   docker compose up --build
   ```
3. Services inclus:
   - API FastAPI
   - MongoDB
   - Redis
   - Nginx reverse proxy

Migration legacy
- Au demarrage, le backend FastAPI:
  - migre les anciens champs Mongo vers le schema canonique;
  - importe les anciens comptes Flask locaux en creant un tenant par utilisateur;
  - importe les anciens routeurs Flask dans un tenant de migration dedie.
- Les routeurs legacy importes restent en `pending_assignment` tant qu'ils ne sont pas reassignes.
- Script de reassignment:
  - `D:\ketaserver\ketamon\backend\scripts\reassign_legacy_routers.py`
  - Exemple:
    ```powershell
    python D:\ketaserver\ketamon\backend\scripts\reassign_legacy_routers.py --list
    python D:\ketaserver\ketamon\backend\scripts\reassign_legacy_routers.py --router-id legacy-router-1 --tenant-slug mon-tenant --owner-email admin@tenant.com
    ```

Tests
- Backend FastAPI:
  ```powershell
  D:\ketaserver\ketamon\backend\.venv\Scripts\python.exe -m pytest -q D:\ketaserver\ketamon\backend\tests
  ```
- Legacy Flask/SQLite:
  ```powershell
  python -m unittest discover -s D:\ketaserver\ketamon\tests -t D:\ketaserver\ketamon -v
  ```

Notes production
- Definir `KETAMON_ENV=production`
- Definir des secrets JWT forts
- Definir `ALLOWED_HOSTS`
- Activer TLS cote reverse proxy
- Laisser `REDIS_URL` actif pour le rate limit distribue
