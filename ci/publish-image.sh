#!/bin/sh
set -eu
: "${IMAGE_REPOSITORY:?}" "${SOURCE_URL:?}" "${COMMIT_SHA:?}" "${REF_KIND:?}" "${REF_NAME:?}"

case "$REF_KIND:$REF_NAME" in
    branch:main) alias=main ;;
    tag:*)
        alias=$REF_NAME
        # Git permits characters and lengths outside the registry tag grammar.
        # Keep main and the SHA tag namespace owned by the branch/commit paths.
        if ! printf '%s\n' "$alias" | LC_ALL=C grep -Eq '^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$' ||
           [ "$alias" = main ] || [ "${alias#sha-}" != "$alias" ] || [ "${alias#git-ref-}" != "$alias" ]; then
            alias="git-ref-$(printf '%s' "$REF_NAME" | sha256sum | cut -c1-32)"
        fi ;;
    *) echo "No container publication for this ref"; exit 0 ;;
esac

image="$IMAGE_REPOSITORY:sha-$COMMIT_SHA"
docker build --platform linux/amd64 --label "org.opencontainers.image.source=$SOURCE_URL" \
    --label "org.opencontainers.image.revision=$COMMIT_SHA" -t "$image" .
# Stream tests into the running image so this also works with a remote Docker daemon.
test_archive=$(mktemp)
trap 'rm -f "$test_archive"' EXIT
tar -cf "$test_archive" -C tests .
docker run -i --rm --platform linux/amd64 --network none --entrypoint sh "$image" \
    -ec 'mkdir -p /tmp/reviewer-tests && tar -xf - -C /tmp/reviewer-tests && python -m unittest discover -s /tmp/reviewer-tests -v' < "$test_archive"
docker tag "$image" "$IMAGE_REPOSITORY:$alias"
docker push "$image"
docker push "$IMAGE_REPOSITORY:$alias"
