# Publishing workflow template:
# 1) Run scripts\scan_public_release.py
# 2) Stop immediately if the scanner returns a non-zero exit code
# 3) Show git status / diff for manual review
# 4) Commit the reviewed files
# 5) Push to the configured Git remote
#
# IMPORTANT:
# - Do not put GitHub tokens/passwords in this file.
# - Keep the repository Private unless a public release has been reviewed.
