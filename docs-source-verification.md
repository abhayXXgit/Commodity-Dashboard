# Source verification log

Checked 2026-09-10. Re-check before trusting any of these.

| Source | Reachable | Verdict |
|---|---|---|
| `open.er-api.com` (FX) | yes | **In use.** Free, no key, daily updates. Cross-checked USD/INR against a second independent provider: 95.121 vs 94.963 — agreement to 0.17%. Terms: exchangerate-api.com/terms |
| `api.exchangerate.host` | yes | Rejected — now requires a paid access key (`missing_access_key`). |
| `lme.com` | **HTTP 403** | Blocked to automated access, and LME data is licensed. Requires an entitlement your organisation holds. Do not attempt to scrape. |
| `nalcoindia.com` | no | Real NALCO domain. Resolves (34.144.206.240) but times out — geo/firewall restricted. |
| `nalcoindia.co.in` | yes | **DO NOT USE — parked domain.** Serves a GoDaddy parking lander (`window._trfd.push({ap:"parking"})`), not NALCO. It is not the company and any figure taken from it would be worthless or an advert. Its `robots.txt` and `llms.txt` are the parking service's, not NALCO's. |

## The rule this illustrates

A source being *reachable* and *allowing robots* says nothing about whether it is
*who it claims to be*. A price imported from a parked domain would enter the system
labelled `VERIFIED_HISTORICAL` and be trusted over demo data by the precedence rule —
which is precisely how a procurement decision gets made on an advert.

Verify the operator of a source before wiring it in, not just its availability.
