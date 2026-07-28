#!/bin/bash
# Check for hardcoded colors and raw px values (exclude tokens.ts)
set -e

HARDCODED_COLORS=$(grep -rn '#[0-9A-Fa-f]\{6\}' src/ --include="*.tsx" --include="*.ts" --include="*.css" 2>/dev/null | grep -v tokens.ts || true)
RAW_PX=$(grep -rn ':\s*\d\+px' src/ --include="*.tsx" --include="*.css" 2>/dev/null | grep -v tokens.ts || true)

if [ -n "$HARDCODED_COLORS" ]; then
  echo "❌ Hardcoded colors found (must use import from @/tokens):"
  echo "$HARDCODED_COLORS"
  exit 1
fi

if [ -n "$RAW_PX" ]; then
  echo "⚠️  Raw px values found (consider using token values):"
  echo "$RAW_PX"
fi

echo "✅ Style check passed"
