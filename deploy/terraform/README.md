# Deploying Argus with Terraform

This configuration runs Argus inside a Kubernetes cluster. It targets Minikube by default.

It creates:

- a namespace, a service account and a Secret for the Gemini keys
- a read-only ClusterRole for the watchers, analyzers and diagnostic agent
- an optional ClusterRole limited to the three approved actions (`enable_remediation`)
- a Deployment, a Service and a volume for the SQLite database
- Prometheus, installed with Helm (`enable_prometheus`)

## Usage

Start a cluster and load the image:

```bash
minikube start
docker build -t argus:latest .
minikube image load argus:latest
```

Deploy:

```bash
cd deploy/terraform
cp terraform.tfvars.example terraform.tfvars
terraform init
terraform plan
terraform apply
```

Open the dashboard with the command shown by Terraform:

```bash
terraform output -raw dashboard_port_forward
```

Remove everything:

```bash
terraform destroy
```

## Notes

- Argus runs one replica because its data lives in a single SQLite file.
- The Gemini keys are stored in the Terraform state, so `*.tfstate` stays out of Git.
- Set `enable_remediation = false` to run Argus in observe-only mode.
