#!/usr/bin/env bash
# Printed on attach. The container has the code and the dependencies; it does
# not have the data, because data/raw is gitignored and the warehouse is 1.8 GB.
# Both are regenerated deterministically from seed 42, so nothing is lost by
# that - but it is ~15 minutes of work and should be a decision, not something
# that happens while you are reading the README.
set -u

UV="$HOME/.local/bin/uv"
bold() { printf '\033[1m%s\033[0m\n' "$1"; }
dim() { printf '\033[2m%s\033[0m\n' "$1"; }

echo
bold "FreshFlow — dark-store expiry & markdown analytics"
dim   "$(git log -1 --format='%h  %s' 2>/dev/null || echo 'no git history')"
echo

if [ -f data/warehouse/freshflow.duckdb ] && [ "$(stat -c%s data/warehouse/freshflow.duckdb 2>/dev/null || echo 0)" -gt 1000000 ]; then
  bold "Warehouse present."
  echo "  python tasks.py test          run the suite"
  echo "  python tasks.py app           serve the Control Tower on :8501"
else
  bold "No warehouse yet. To build one from scratch (~15 min):"
  echo
  echo "  $UV run python tasks.py simulate --days 365 --seed 42   # ~13 min, writes data/raw"
  echo "  $UV run python tasks.py build --full-refresh            # ~4 min, 415 dbt nodes"
  echo "  $UV run python tasks.py forecast                        # LightGBM + backtest"
  echo "  $UV run python tasks.py expiry-risk"
  echo "  $UV run python tasks.py elasticity"
  echo "  $UV run python tasks.py markdown"
  echo "  $UV run python tasks.py deal-slots"
  echo "  $UV run python tasks.py transfers"
  echo "  $UV run python tasks.py newsvendor"
  echo "  $UV run python tasks.py policy-bundle"
  echo
  dim "  The simulator is seeded, so this reproduces the committed figures exactly:"
  dim "  Rs 43.4305 Cr net revenue, 4,259,103 order items, 23 elasticity cells."
fi

echo
bold "Then, to publish what the live app reads:"
echo "  $UV run python tasks.py demo-slice     # <80 MB slice, 5 stores x 90 days"
echo "  $UV run python tasks.py publish-demo   # uploads to the GitHub Release"
echo
dim "Live app:  https://freshflow-qcommerce-analytics-b2ozx2naawfh7gxubum6gm.streamlit.app/"
dim "dbt docs:  https://yadnesh02.github.io/freshflow-qcommerce-analytics/"
echo
