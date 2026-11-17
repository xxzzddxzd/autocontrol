"""Local auto-execution console.

The console schedules rounds of an external game worker, streams its progress
events, archives fixed-field results and keeps one Lark to-do card per service
day up to date. Display state never authorizes or skips a game action: every
worker run reads the live server state itself.
"""

__version__ = '0.1.0'
