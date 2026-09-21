"""Pre-push safety scan.

Add checks here for values that must never appear in a public release, e.g.
- real Controller IPs
- passwords/tokens/private keys
- conf.json
- .streamlit/secrets.toml
- organization-specific role/group names

Exit with a non-zero status when a forbidden value is found so the publish
script can stop before git push.
"""

# TODO: Implement repository scanning before publishing.
