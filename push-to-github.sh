#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Push this project to a GitHub repository you have already created.
#
#   ./push-to-github.sh https://github.com/<you>/commodity-dashboard.git
#
# Your credentials are entered by you, into git, in your own terminal.
# Nothing in this script stores, prints or transmits them anywhere else.
# ---------------------------------------------------------------------------
set -e
cd "$(dirname "$0")"

URL="$1"
if [ -z "$URL" ]; then
  echo ""
  echo "  Usage: ./push-to-github.sh <repository-url>"
  echo ""
  echo "  First create an EMPTY repo at https://github.com/new"
  echo "    - name it            : commodity-dashboard"
  echo "    - visibility         : Public (or Private)"
  echo "    - do NOT tick        : Add a README / .gitignore / licence"
  echo ""
  echo "  Then copy the URL it shows you and run:"
  echo "    ./push-to-github.sh https://github.com/YOURNAME/commodity-dashboard.git"
  echo ""
  exit 1
fi

echo ""
echo "  Repository : $URL"
echo "  Commit     : $(git log -1 --format='%h  %s' 2>/dev/null || echo 'none')"
echo "  Files      : $(git ls-files | wc -l | tr -d ' ')"
echo ""

if git remote | grep -q '^origin$'; then
  git remote set-url origin "$URL"
  echo "  updated existing 'origin' remote"
else
  git remote add origin "$URL"
  echo "  added 'origin' remote"
fi

echo ""
echo "  Pushing. GitHub will ask for:"
echo "     Username : your GitHub username"
echo "     Password : a Personal Access Token  (NOT your account password)"
echo ""
echo "  No token yet?  github.com/settings/tokens  ->  Generate new token (classic)"
echo "                 tick the 'repo' scope, copy it, paste it as the password."
echo "                 It will not echo as you paste - that is normal."
echo ""

git push -u origin main

echo ""
echo "  Done. Your repository:"
echo "    ${URL%.git}"
echo ""
