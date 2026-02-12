# Security Policy

## Supported Versions

The latest release is supported for security fixes.

## Safe Defaults

- Dashboard now binds to `127.0.0.1` by default.

## Deployment Guidance

- Prefer localhost usage for personal setups.
- If you bind to non-local hosts (for example `--host 0.0.0.0`), secure access at the network or reverse-proxy layer.
- Keep the service behind a trusted reverse proxy if internet-exposed.

## Reporting a Vulnerability

Please open a private report if possible, or contact the maintainer directly before public disclosure.
