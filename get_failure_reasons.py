import json
from db import rest_get
import datetime

today = datetime.date.today().isoformat()
failed = rest_get('donations', params={'status': 'eq.failed', 'created_at': f'gte.{today}'})

for d in failed:
    meta = d.get('metadata') or {}
    print(f"\nDonation ID: {d.get('id')}")
    print(f"Name: {d.get('first_name', '')} {d.get('last_name', '')}")
    print(f"Error Message: {d.get('error_message') or 'None'}")
    print(f"Stripe Error: {meta.get('stripe_error') or meta.get('error') or 'None'}")
    print(f"Decline Code: {meta.get('decline_code') or 'None'}")
    print(f"Full Metadata: {json.dumps(meta, indent=2)}")
