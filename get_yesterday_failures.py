from db import rest_get
import datetime

today = datetime.date.today()
yesterday = today - datetime.timedelta(days=1)

failed = rest_get('donations', params={
    'status': 'eq.failed', 
    'created_at': f'gte.{yesterday.isoformat()}'
})

# Filter for strictly yesterday
yesterday_failures = [d for d in failed if d.get('created_at', '').startswith(yesterday.isoformat())]

print(f"Total failures yesterday ({yesterday.isoformat()}): {len(yesterday_failures)}")
for d in yesterday_failures:
    print(f"- {d.get('first_name', '')} {d.get('last_name', '')}: {d.get('amount')} {d.get('currency')} (Error: {d.get('comment')})")
