# pr-agent on Kubernetes with Flux

This mirrors `compose.yaml`: one pod with the `setup`, `sync`, and `reviewer`
containers sharing the `state`, `repos`, and `model-auth` volumes.

## Deploy

Copy this `k8s/` directory into the root of your GitOps repository, then point
Flux at it:

```sh
kubectl apply -f k8s/flux.yaml
kubectl -n pr-agent get pods
```

Change `sourceRef.name` in `k8s/flux.yaml` if your GitRepository is not named
`flux-system`.

## Configure

Attach to the setup console and press **Enter**:

```sh
kubectl attach -it deploy/pr-agent
```

It prints a one-time URL. In a second terminal, forward the setup port:

```sh
kubectl port-forward svc/pr-agent 8765:8765
```

Open the printed URL in your browser. Keep the local port at `8765`; the
wizard checks the `Host` header. Detach with **Ctrl-P**, **Ctrl-Q**.

## Notes

- Image: `ghcr.io/medici-finance/assay-pr-agent:main` (linux/amd64). Pin a
  digest or `sha-<commit>` tag for reproducible deployments.
- If the GHCR package is private, uncomment `imagePullSecrets` and create the
  pull secret.
- The three PVCs are `ReadWriteOnce`; the Deployment uses one replica and the
  `Recreate` strategy so the volumes are never attached twice.
- The setup wizard writes credentials to `/state` and `/model-auth`. For
  environment-only credentials, see the Kubernetes Secret example in the main
  `README.md`.
- The one-time setup URL is printed to the attached terminal and may appear in
  container logs; treat it as a credential and restrict log access.
- The compose `sync` healthcheck is intentionally not a Kubernetes readiness
  probe: it would mark the pod not-ready before setup completes and block the
  setup Service.
