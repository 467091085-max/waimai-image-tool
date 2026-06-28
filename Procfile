web: gunicorn --chdir api-server app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --worker-class gthread --timeout 60
worker: python worker/worker.py
