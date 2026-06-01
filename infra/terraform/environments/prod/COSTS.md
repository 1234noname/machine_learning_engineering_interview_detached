# AVSA prod — back-of-envelope cost estimate

All figures are approximate, based on us-central1 pricing as of mid-2026.
Actual costs depend on utilisation, sustained-use discounts, and GCP pricing changes.

## Monthly baseline (always-running resources)

| Resource | Spec | Cost/month |
|---|---|---|
| GKE cluster management fee | Regional cluster (Autopilot not used) | $73 |
| CPU node pool | `e2-standard-4`, 1–3 nodes (autoscaling), min=1 | ~$100–$300 |
| Cloud SQL | `db-g1-small`, Postgres 15, 10 GB storage | ~$25 |
| Artifact Registry | Docker repo, 10 GB storage | ~$1 |
| Secret Manager | 3 secrets, <10K accesses/month | ~$0.18 |
| Cloud NAT | Regional, ~1 GB egress | ~$1 |
| Cloud Logging | Free tier covers v1 volume | $0 |
| VPC / networking | Minimal egress at v1 | ~$5 |
| **Subtotal (no GPU)** | | **~$205–$405/month** |

## GPU node pool (autoscale-to-zero)

The GPU node pool (`n1-standard-8` + `nvidia-tesla-t4 × 1`, preemptible) scales to zero when no GPU workload is running. Cost is incurred only while nodes are active.

| Mode | Rate | Notes |
|---|---|---|
| Node running (preemptible) | ~$0.37/hr | `n1-standard-8` ($0.24/hr) + T4 GPU ($0.11/hr preemptible) |
| Node running (on-demand) | ~$1.15/hr | If preemptible unavailable and GCP falls back |
| Scaled to zero | **$0.00/hr** | `min_node_count = 0` — no nodes, no cost |

### Example GPU cost scenarios

| Scenario | GPU hours/month | Cost/month |
|---|---|---|
| Dev-only (scaled to zero) | 0 | $0 |
| 2 hours/day load testing | ~60 hrs | ~$22 |
| Continuous ViT serving (1 GPU, 24×7) | ~730 hrs | ~$270 (preemptible) |
| Continuous ViT serving (on-demand) | ~730 hrs | ~$840 |

## Total range

| Scenario | Monthly estimate |
|---|---|
| Infrastructure only, GPU off | $205 |
| Infrastructure + light GPU use (load tests only) | ~$227 |
| Infrastructure + continuous GPU serving (preemptible) | ~$475 |
| Infrastructure + continuous GPU serving (on-demand) | ~$1,045 |

## Cost controls in place

1. **GPU pool autoscales to zero** (`min_node_count = 0`) — no idle GPU cost.
2. **GPU pool is preemptible** — ~68% discount vs on-demand; GCP may reclaim nodes.
3. **CPU pool min=1** — one always-on node for system workloads (Prometheus, ESO).
4. **Cloud SQL `db-g1-small`** — smallest tier with shared-CPU; upgrade for real traffic.
5. **GCP budget alarm** recommended: set at $600/month to catch unexpected GPU-on events.

## Budget alarm (recommended, not Terraform-managed)

```bash
gcloud billing budgets create \
  --billing-account=<BILLING_ACCOUNT_ID> \
  --display-name="AVSA prod budget alarm" \
  --budget-amount=600USD \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.9 \
  --threshold-rule=percent=1.0
```

## References

- [GCP Compute Engine pricing](https://cloud.google.com/compute/vm-instance-pricing)
- [GCP GPU pricing](https://cloud.google.com/compute/gpus-pricing)
- [Cloud SQL pricing](https://cloud.google.com/sql/pricing)
- [ADR 0003](../../docs/adr/0003-gcp-as-deploy-target.md) — cost acknowledgement ($200-400/month at v1)
