terraform {
  required_version = ">= 1.6.0"

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.30"
    }
  }
}

locals {
  service   = "payments-api"
  namespace = "payments"
}

resource "kubernetes_namespace" "payments" {
  metadata {
    name = local.namespace

    labels = {
      app = local.service
    }
  }
}
