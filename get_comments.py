from db import rest_get
import datetime

today = datetime.date.today().isoformat()
failed = rest_get('donations', params={'status': 'eq.failed', 'created_at': f'gte.{today}'})

for d in failed:
    print(f"ID: {d.get('id')}")
    print(f"Name: {d.get('first_name', '')} {d.get('last_name', '')}")
    print(f"Comment/Error: {d.get('comment')}\n")
