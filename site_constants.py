"""Platform-level constants for the root public site."""

ROOT_ORG_ID = "00000000-0000-4000-8000-000000000001"
ROOT_CAMPAIGN_ID = "00000000-0000-4000-8000-000000000002"
ROOT_CAMPAIGN_SLUG = "sudan"

ROOT_BODY_HTML = """
<p><strong>More People Are in Famine in Sudan Than The Rest of The World Combined.</strong> 😞</p>
<p>Now just imagine…</p>
<p>A mother walked into a hospital in El Fasher holding her pregnant belly.</p>
<p>She was about to give birth, and she needed HELP.</p>
<p><strong>But what happened next will haunt you.</strong></p>
<p>Armed men stormed through the doors and attacked the hospital.</p>
<p>They slaughtered EVERYONE inside.</p>
<p>Doctors.</p>
<p>Nurses.</p>
<p>Pregnant women waiting for care.</p>
<p><strong>💔 500 people killed. In one building. In one afternoon.</strong></p>
<p>But here's what makes it worse…</p>
<p>The women who tried to run were <strong>hunted down</strong> and <strong>raped</strong> in the streets.</p>
<p><strong>Right now, at this very moment in Sudan:</strong></p>
<p>😭 <strong>12 million people are fleeing:</strong> That's every single person in London suddenly homeless, running, nowhere to go</p>
<p>😭 <strong>24 million people are starving:</strong> Not hungry, starving… which means their bodies are eating themselves to survive</p>
<p>😭 <strong>635,000 people are already living in famine</strong>: More than the rest of the world combined.</p>
<p>😭 <strong>70% of hospitals have been bombed into dust.</strong></p>
<p>😭 <strong>150,000 people have died in Sudan.</strong></p>
<p>And then there's what's happening to the children.</p>
<p><strong>💔</strong> UNICEF documented armed militias raping children as young as ONE YEAR OLD.</p>
<p>Let that sink all the way in.</p>
<p>Your donation today is the difference between:</p>
<p>✨ A child eating today and starving today.</p>
<p>✨ A woman giving birth with medical care or bleeding to death alone.</p>
<p>✨ A family drinking clean water or dying from cholera.</p>
<p>But if you close this page and go back to scrolling:</p>
<p>👉 More children will be raped.</p>
<p>👉 More mothers will bury babies they couldn't feed.</p>
<p>👉 More families will starve in silence while the world looks at something else.</p>
<p>You can't save everyone. But you can save someone.</p>
<p><strong>And to that someone in Sudan, YOU are everything.</strong></p>
<p>The world stayed silent about Sudan.</p>
<p>But you don't have to.</p>
<p>💛 Please donate now and be the help Sudan is desperately waiting for.</p>
""".strip()

DEFAULT_CAMPAIGN_CONTENT = {
    "title": "Sudan Needs You - the world's worst humanitarian crisis 😔💔",
    "caption": "More People Are in Famine in Sudan Than The Rest of The World Combined. 😔",
    "body_html": ROOT_BODY_HTML,
    "dedication_hint": (
        "After completing your donation, you will see options to write a personalized message, "
        "send a card with your dedication, and schedule it to be sent on a specific date and time."
    ),
    "primary_color": "#3872DC",
    "logo_url": "/assets/logo.avif",
    "logo_width": 160,
    "logo_height": 56,
    "hero_url": "/assets/herobanner.jfif",
    "hero_width": 1248,
    "hero_height": 702,
    "hero_alt": "A malnourished child sitting on a bed in a healthcare facility. The image has text reading 'SAVE LIVES IN SUDAN'.",
    "favicon_url": "/icon.png",
}
