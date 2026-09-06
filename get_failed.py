from db import rest_get
import datetime
today = datetime.date.today().isoformat()
failed = rest_get('donations', params={'status': 'eq.failed', 'created_at': f'gte.{today}'})
for d in failed:
    print(f"- {d.get('first_name', '')} {d.get('last_name', '')} ({d.get('email', 'No email')}): {d.get('amount')} {d.get('currency')} via {d.get('payment_method')} (ID: {d.get('id')})")
