from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException


def load_kube_client() -> client.CoreV1Api:
    """
    Loads Kubernetes config safely:
    - in-cluster when running inside k8s
    - kubeconfig when running locally
    """
    try:
        config.load_incluster_config()
    except ConfigException:
        config.load_kube_config()

    return client.CoreV1Api()
