#!/usr/bin/env bash
# Publish the verified assets for the exact commit built by CI.
set -euo pipefail

: "${GH_REPO:?Set GH_REPO}" "${RELEASE_TAG:?Set RELEASE_TAG}" "${RELEASE_TARGET:?Set RELEASE_TARGET}"
assets_dir=${1:?Supply the release assets directory}
notes_file=$(cd "$(dirname "${2:?Supply the release notes file}")" && pwd)/$(basename "$2")
python3 "$(dirname "$0")/release.py" verify --tag "$RELEASE_TAG" --commit "$RELEASE_TARGET" --input-dir "$assets_dir" --repository "$GH_REPO"
cd "$assets_dir"
assets=("./home-screen-organizer-${RELEASE_TAG#v}-macos-arm64.dmg"
        "./home-screen-organizer-${RELEASE_TAG#v}-macos-arm64.zip"
        "./home-screen-organizer-${RELEASE_TAG#v}-macos-x86_64.dmg"
        "./home-screen-organizer-${RELEASE_TAG#v}-macos-x86_64.zip"
        "./home-screen-organizer-${RELEASE_TAG#v}-macos.mcpb" "./home-screen-organizer-${RELEASE_TAG#v}-corresponding-source.tar.gz" "server.json" "SHA256SUMS.txt")
for asset in "${assets[@]}"; do test -f "$asset"; done
shasum -a 256 --check SHA256SUMS.txt

releases=$(gh api --paginate "repos/$GH_REPO/releases?per_page=100" \
  --jq '.[] | [.tag_name, .draft] | @tsv')
draft=
while IFS=$'\t' read -r tag is_draft; do
  if [[ "$tag" == "$RELEASE_TAG" ]]; then draft=$is_draft; fi
done <<< "$releases"
if [[ "$draft" == false ]]; then
  echo "$RELEASE_TAG is published. Increment project.version for the next release."
  exit 0
fi

refs=$(gh api "repos/$GH_REPO/git/matching-refs/tags/$RELEASE_TAG" --jq '.[].ref')
while IFS= read -r ref; do
  if [[ "$ref" == "refs/tags/$RELEASE_TAG" ]]; then
    target=$(gh api "repos/$GH_REPO/commits/$RELEASE_TAG" --jq .sha)
    if [[ "$target" != "$RELEASE_TARGET" ]]; then
      echo "Set a new project version: $RELEASE_TAG points to another commit." >&2
      exit 1
    fi
  fi
done <<< "$refs"

rendered_notes=$(mktemp)
trap 'rm -f "$rendered_notes"' EXIT
source_url="https://github.com/$GH_REPO/releases/download/$RELEASE_TAG/home-screen-organizer-${RELEASE_TAG#v}-corresponding-source.tar.gz"
sed "s|{{SOURCE_URL}}|$source_url|g" "$notes_file" > "$rendered_notes"
notes_file="$rendered_notes"

prerelease=false
if [[ "$RELEASE_TAG" == *-* ]]; then prerelease=true; fi
if [[ "$draft" == true ]]; then
  gh release upload "$RELEASE_TAG" --clobber "${assets[@]}"
  gh release edit "$RELEASE_TAG" --draft=false --target "$RELEASE_TARGET" \
    --title "Screen Organizer $RELEASE_TAG" --notes-file "$notes_file" \
    --prerelease="$prerelease"
else
  gh release create "$RELEASE_TAG" --target "$RELEASE_TARGET" \
    --title "Screen Organizer $RELEASE_TAG" --notes-file "$notes_file" \
    --prerelease="$prerelease" "${assets[@]}"
fi
