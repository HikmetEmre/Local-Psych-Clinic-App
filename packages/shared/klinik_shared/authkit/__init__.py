"""Authentication shared by both tiers: bcrypt + TOTP, server-side sessions, CSRF, lockout.

Each tier has its own user table and credentials (so each works when the other is
down); only the user UUIDs are the same on both sides.
"""
