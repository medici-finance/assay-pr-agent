#!/bin/sh
set -eu
: "${IMAGE_REPOSITORY:?}" "${SOURCE_URL:?}" "${COMMIT_SHA:?}" "${REF_KIND:?}" "${REF_NAME:?}"

case "$REF_KIND:$REF_NAME" in
    branch:main) aliases="main latest" ;;
    tag:*)
        alias=$REF_NAME
        # Git permits characters and lengths outside the registry tag grammar.
        # Keep main, latest and the SHA tag namespace owned by the branch/commit paths.
        if ! printf '%s\n' "$alias" | LC_ALL=C grep -Eq '^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$' ||
           [ "$alias" = main ] || [ "$alias" = latest ] || [ "${alias#sha-}" != "$alias" ] || [ "${alias#git-ref-}" != "$alias" ]; then
            alias="git-ref-$(printf '%s' "$REF_NAME" | sha256sum | cut -c1-32)"
        fi
        aliases=$alias ;;
    *) echo "No container publication for this ref"; exit 0 ;;
esac

# Baked into the image so the container can report its commit, tags and build time.
built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
image="$IMAGE_REPOSITORY:sha-$COMMIT_SHA"
docker build --platform linux/amd64 \
    --label "org.opencontainers.image.source=$SOURCE_URL" \
    --label "org.opencontainers.image.revision=$COMMIT_SHA" \
    --label "org.opencontainers.image.created=$built_at" \
    --build-arg "REVIEW_BUILD_SHA=$COMMIT_SHA" \
    --build-arg "REVIEW_BUILD_TAGS=$aliases" \
    --build-arg "REVIEW_BUILD_TIMESTAMP=$built_at" \
    -t "$image" .
# Stream the tests, the k8s manifest and the prompt-capture script they exercise into the
# running image so this also works with a remote Docker daemon.
test_archive=$(mktemp)
trap 'rm -f "$test_archive"' EXIT
tar --exclude='__pycache__' -cf "$test_archive" tests k8s ci/capture-prompt.py
# Test the hardened runtime with networking disabled. Forge fixtures use loopback.
docker run -i --rm --platform linux/amd64 --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --tmpfs /tmp:mode=1777,size=1g,exec \
    --entrypoint sh "$image" \
    -ec 'mkdir -p /tmp/reviewer-tests && tar -xf - -C /tmp/reviewer-tests && python -m unittest discover -s /tmp/reviewer-tests/tests -v' < "$test_archive"
# shellcheck disable=SC2086  # aliases are space-separated registry tags
for alias in $aliases; do
    docker tag "$image" "$IMAGE_REPOSITORY:$alias"
done
docker push "$image"
# shellcheck disable=SC2086
for alias in $aliases; do
    docker push "$IMAGE_REPOSITORY:$alias"
done
