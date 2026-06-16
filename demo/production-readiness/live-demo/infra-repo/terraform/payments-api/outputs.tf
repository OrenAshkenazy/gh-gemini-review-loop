output "payments_namespace" {
  value = kubernetes_namespace.payments.metadata[0].name
}
