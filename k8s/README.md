# pr-agent on Kubernetes with Flux

This mirrors `compose.yaml`: one pod with the `setup`, `sync`, and `reviewer`
containers sharing the `state`, `repos`, and `model-auth` volumes. Instead of
compose's attachable manager, the `setup` container runs the browser wizard
directly as a sidecar.

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

The wizard exits after each session and the sidecar restarts it, so the setup
port is always available. Each session generates a random **setup key** and
prints it to the log. Read the current one:

```sh
kubectl -n pr-agent logs deploy/pr-agent -c setup | grep 'Setup key'
```

In a second terminal, forward the setup port, open `http://127.0.0.1:8765/` in
your browser and enter the key. The page keeps it in a session cookie; click
**Print key to console** to have the current key printed to the log again. Keep
the local port at `8765`; the wizard checks the `Host` header.

```sh
kubectl port-forward -n pr-agent svc/pr-agent 8765:8765
```

If you choose provider **login** (Cursor, Claude or Codex), the browser asks you
to continue in the installer terminal when the provider CLI signs in:

```sh
kubectl attach -it -n pr-agent deploy/pr-agent -c setup
```

Finish the sign-in there and detach with **Ctrl-P**, **Ctrl-Q**. `sync` and
`reviewer` wait for the first setup to complete and reload when the wizard saves
a configuration.

To change projects or credentials later, fetch the key from the log again. To
switch the provider account, request a fresh sign-in and end the current session
with **Quit setup** in the browser; the next session runs with `--reauth`:

```sh
kubectl exec -n pr-agent deploy/pr-agent -c setup -- touch /state/.reauth
```

## Notes

- Image: `ghcr.io/medici-finance/assay-pr-agent:main` (linux/amd64); `latest` is an
  alias of `main`. Pin a digest or `sha-<commit>` tag for reproducible deployments.
- If the GHCR package is private, uncomment `imagePullSecrets` and create the
  pull secret.
- The three PVCs are `ReadWriteOnce`; the Deployment uses one replica and the
  `Recreate` strategy so the volumes are never attached twice.
- The setup wizard writes credentials to `/state` and `/model-auth`. For
  environment-only credentials, see the Kubernetes Secret example in the main
  `README.md`.
- Each wizard session prints a random setup key to the `setup` container log.
  It is valid until that session ends, so treat container logs as a credential
  and restrict log access.
- The compose `sync` healthcheck is intentionally not a Kubernetes readiness
  probe: it would mark the pod not-ready before setup completes and block the
  setup Service.
- The containers run with a read-only root filesystem and a 1Gi `emptyDir` on
  `/tmp`. Everything that persists lives on the three PVCs.
- `pr-agent-egress` is an egress-only NetworkPolicy: DNS on port 53 to any
  destination (the kube-dns Service IP is cluster-specific, some CNIs evaluate
  egress before Service DNAT and NodeLocal DNSCache uses a link-local node
  address), plus the public internet, where the forge, model providers and
  advisory databases live. Private ranges (RFC1918, CGNAT `100.64.0.0/10`,
  link-local including cloud metadata) are blocked. If your forge, GitLab
  instance or proxy is in one of them, drop it from `except`. NetworkPolicy
  cannot restrict traffic from a pod to the node it runs on, so a private
  service listening there is a host-level concern, not this policy's.
  Ingress is not restricted, so `kubectl port-forward` works and you can expose
  the setup port with your own Traefik `IngressRoute` (limit it to the Traefik
  namespace).
