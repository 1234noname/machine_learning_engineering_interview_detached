resource "kubernetes_namespace" "app" {
  metadata {
    name = "avsa-${var.environment}${local.suffix}"
    labels = {
      "app.kubernetes.io/managed-by" = "Helm"
    }
    annotations = {
      "meta.helm.sh/release-name"      = "avsa-${var.environment}${local.suffix}"
      "meta.helm.sh/release-namespace" = "avsa-${var.environment}${local.suffix}"
    }
  }
}

resource "helm_release" "app" {
  name             = "avsa-${var.environment}${local.suffix}"
  chart            = "${path.module}/../../../../helm"
  namespace        = kubernetes_namespace.app.metadata[0].name
  create_namespace = false
  wait             = false
  cleanup_on_fail  = true
  values           = [file("${path.module}/../../../../helm/values.gke.yaml")]

  set {
    name  = "image_api.repository"
    value = local.api_image_repo
  }
  set {
    name  = "image_api.tag"
    value = local.app_tag
  }
  set {
    name  = "image_model.repository"
    value = local.model_image_repo
  }
  set {
    name  = "image_model.tag"
    value = local.app_tag
  }
  set {
    name  = "image_shopper.repository"
    value = local.shopper_image_repo
  }
  set {
    name  = "image_shopper.tag"
    value = local.app_tag
  }
  set {
    name  = "image_batcher.repository"
    value = local.batcher_image_repo
  }
  set {
    name  = "image_batcher.tag"
    value = local.app_tag
  }
  set {
    name  = "image_orchestrator.repository"
    value = local.orchestrator_image_repo
  }
  set {
    name  = "image_orchestrator.tag"
    value = local.app_tag
  }
  set {
    name  = "environment"
    value = var.environment
  }
  set {
    name  = "projectId"
    value = var.project_id
  }
  set {
    name  = "region"
    value = var.region
  }
}

locals {
  suffix                = var.name_suffix != "" ? "-${var.name_suffix}" : ""
  registry              = var.artifact_registry_host
  api_image_repo        = "${local.registry}/avsa/api"
  model_image_repo      = "${local.registry}/avsa/model"
  shopper_image_repo    = "${local.registry}/avsa/shopper"
  batcher_image_repo    = "${local.registry}/avsa/batcher"
  orchestrator_image_repo = "${local.registry}/avsa/orchestrator"
  app_tag               = var.app_image
}

data "kubernetes_ingress_v1" "app" {
  metadata {
    name      = "avsa-${var.environment}${local.suffix}"
    namespace = kubernetes_namespace.app.metadata[0].name
  }

  depends_on = [helm_release.app]
}
