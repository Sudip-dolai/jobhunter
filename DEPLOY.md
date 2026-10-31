# Deploy to Render (web) + free scheduled job finder

1. Push this folder to a GitHub repo (see the commands in the chat). `.env` is git-ignored, so secrets stay on your PC.
2. Render > New > Blueprint > pick the repo. Fill in DATABASE_URL, SECRET_KEY, TELEGRAM_BOT_TOKEN when asked.
   DATABASE_URL must look like: postgresql+psycopg2://USER:PASSWORD@HOST/neondb?sslmode=require  (direct host, no -pooler)
3. When the site is live: create your account, then set ALLOW_SIGNUP=0 in Render > Environment so nobody else can sign up.
4. Job finding on a schedule (pick ONE):
   - Free: GitHub repo > Settings > Secrets and variables > Actions: add DATABASE_URL, SECRET_KEY, TELEGRAM_BOT_TOKEN.
     The workflow in .github/workflows/pipeline.yml then runs every 6 hours (Actions tab > find-jobs > Run workflow to run now).
   - Paid: uncomment the worker block in render.yaml.
5. New companies: run `python -m app.harvest` on your PC, then commit and push companies.json (the server reads it from the repo).
