"""The web board: every project's daily checklist on one page, behind Feishu login.

It takes the place of the bot's Lark cards. It reads only local files (the
project list, the console status, each project's saved card snapshot, metrics
and logs), so viewing it never spends the tenant's monthly Feishu API calls.
Login uses Feishu's web authorization (an authorization code with PKCE). Those
are authentication APIs, which Feishu does not count against that quota.

The console runs it as a helper process (``run.py dashboard``) that listens on
127.0.0.1 only. A tunnel such as cloudflared makes it reachable over HTTPS.
"""
