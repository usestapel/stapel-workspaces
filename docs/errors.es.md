# Errors — Español

`70` error keys. Canonical texts live in the code (`register_service_errors`); localized texts in `translations/errors.es.json`.

| Código | Estado | Parámetros | Acción | Texto |
|---|---|---|---|---|
| `error.400.already_workspace_member` | 400 | — | `fix_input` | El usuario ya es miembro de este espacio de trabajo |
| `error.400.bad_request` | 400 | — | `fix_input` | Solicitud incorrecta |
| `error.400.captcha_invalid` | 400 | — | `retry` | La verificación del captcha ha fallado. Inténtalo de nuevo. |
| `error.400.captcha_required` | 400 | — | `retry` | Se requiere el token del captcha. |
| `error.400.display_name_emoji` | 400 | — | `fix_input` | El nombre para mostrar no puede contener emojis |
| `error.400.display_name_forbidden_chars` | 400 | — | `fix_input` | El nombre para mostrar contiene caracteres no permitidos |
| `error.400.display_name_invisible_chars` | 400 | — | `fix_input` | El nombre para mostrar contiene caracteres invisibles |
| `error.400.display_name_too_short` | 400 | — | `fix_input` | El nombre para mostrar debe tener al menos 2 caracteres |
| `error.400.expected_list` | 400 | — | `fix_input` | Se esperaba una lista de elementos |
| `error.400.field.blank` | 400 | `field` | `fix_input` | {field} no puede estar vacío |
| `error.400.field.does_not_exist` | 400 | `field` | `fix_input` | {field} no existe |
| `error.400.field.invalid` | 400 | `field` | `fix_input` | {field} no es válido |
| `error.400.field.invalid_choice` | 400 | `field` | `fix_input` | {field} no es una opción válida |
| `error.400.field.max_length` | 400 | `field`, `max_length` | `fix_input` | {field} debe tener como máximo {max_length} caracteres |
| `error.400.field.max_value` | 400 | `field`, `max_value` | `fix_input` | {field} debe ser como máximo {max_value} |
| `error.400.field.min_length` | 400 | `field`, `min_length` | `fix_input` | {field} debe tener al menos {min_length} caracteres |
| `error.400.field.min_value` | 400 | `field`, `min_value` | `fix_input` | {field} debe ser como mínimo {min_value} |
| `error.400.field.null` | 400 | `field` | `fix_input` | {field} no puede ser nulo |
| `error.400.field.required` | 400 | `field` | `fix_input` | {field} es obligatorio |
| `error.400.field.unique` | 400 | `field` | `fix_input` | {field} debe ser único |
| `error.400.invalid_ad_id` | 400 | — | `fix_input` | ID de anuncio no válido |
| `error.400.invalid_provision_username` | 400 | — | `fix_input` | Nombre de usuario no válido para una cuenta aprovisionada |
| `error.400.invalid_role` | 400 | — | `fix_input` | Rol no válido |
| `error.400.invitation_already_used` | 400 | — | `fix_input` | La invitación ya se ha utilizado |
| `error.400.invitation_declined` | 400 | — | `contact_support` | La invitación ha sido rechazada |
| `error.400.invitation_expired` | 400 | — | `contact_support` | La invitación ha caducado |
| `error.400.invitation_revoked` | 400 | — | `contact_support` | La invitación ha sido revocada |
| `error.400.validation_error` | 400 | — | `fix_input` | Error de validación |
| `error.400.verification_failed` | 400 | — | `verify` | La verificación ha fallado |
| `error.400.verification_invalid_factor` | 400 | — | `verify` | Este factor de verificación no está disponible |
| `error.400.workspace_slug_taken` | 400 | — | `fix_input` | El slug del espacio de trabajo ya está en uso |
| `error.401.unauthorized` | 401 | — | `reauthenticate` | Se requiere autenticación |
| `error.402.entitlement_required` | 402 | — | `fix_input` | El plan del propietario del espacio de trabajo no incluye esta función |
| `error.402.member_limit_reached` | 402 | `limit` | `fix_input` | Se ha alcanzado el límite de miembros del espacio de trabajo ({limit}) |
| `error.402.payment_required` | 402 | — | `retry` | Se requiere pago |
| `error.403.forbidden` | 403 | — | `retry` | No tienes permiso para realizar esta acción |
| `error.403.forbidden_workspace` | 403 | — | `contact_support` | No tienes acceso a este espacio de trabajo |
| `error.403.last_owner_cannot_be_removed` | 403 | — | `fix_input` | El último propietario no se puede eliminar; primero transfiere la propiedad |
| `error.403.membership_suspended` | 403 | `reason` | `fix_input` | Tu pertenencia a este espacio de trabajo está suspendida ({reason}) |
| `error.403.missing_capability` | 403 | `capability` | `contact_support` | Tu rol no incluye la capacidad {capability} en este espacio de trabajo |
| `error.403.network_blocked` | 403 | — | `contact_support` | No se permiten solicitudes desde esta red. |
| `error.403.role_exceeds_inviter_rank` | 403 | `role` | `fix_input` | No puedes conceder un rol superior al tuyo ({role}) |
| `error.403.verification_enrollment_required` | 403 | — | `verify` | Es necesario registrar un factor de verificación. |
| `error.403.verification_required` | 403 | — | `verify` | Se requiere verificación adicional |
| `error.403.workspace_creation_closed` | 403 | — | `contact_support` | Esta instancia no te permite crear espacios de trabajo |
| `error.404.ad_not_found` | 404 | — | `retry` | Anuncio no encontrado |
| `error.404.invitation_not_found` | 404 | — | `fix_input` | Invitación no encontrada |
| `error.404.member_not_found` | 404 | — | `fix_input` | Miembro no encontrado en este espacio de trabajo |
| `error.404.not_found` | 404 | — | `retry` | Recurso solicitado no encontrado |
| `error.404.verification_challenge_not_found` | 404 | — | `verify` | Desafío de verificación no encontrado o caducado |
| `error.404.workspace_not_found` | 404 | — | `fix_input` | Espacio de trabajo no encontrado |
| `error.405.method_not_allowed` | 405 | — | `retry` | Método no permitido |
| `error.406.not_acceptable` | 406 | — | `retry` | No aceptable |
| `error.408.request_timeout` | 408 | — | `retry` | Tiempo de espera de la solicitud agotado |
| `error.409.conflict` | 409 | — | `fix_input` | El recurso ya existe |
| `error.409.email_already_registered` | 409 | — | `reauthenticate` | Ya existe una cuenta con este correo electrónico: inicia sesión en su lugar |
| `error.410.gone` | 410 | — | `retry` | El recurso se ha eliminado permanentemente |
| `error.413.payload_too_large` | 413 | — | `retry` | El cuerpo de la solicitud es demasiado grande |
| `error.415.unsupported_media_type` | 415 | — | `retry` | Tipo de contenido no compatible |
| `error.422.unprocessable_entity` | 422 | — | `wait_and_retry` | Entidad no procesable |
| `error.423.locked` | 423 | — | `wait_and_retry` | El recurso está bloqueado |
| `error.423.verification_locked` | 423 | — | `wait_and_retry` | Demasiados intentos fallidos — verificación bloqueada |
| `error.429.invitation_grant_pending` | 429 | `retry_after` | `wait_and_retry` | El enlace de acceso de esta invitación sigue siendo válido; puedes solicitar otro en {retry_after} segundos |
| `error.429.invitation_resend_cooldown` | 429 | `retry_after` | `wait_and_retry` | Esta invitación se envió por correo hace poco; puedes volver a enviarla en {retry_after} segundos |
| `error.429.rate_limit` | 429 | `retry_after_minutes` | `wait_and_retry` | Demasiados intentos. Inténtalo de nuevo en {retry_after_minutes} minutos. |
| `error.429.too_many_requests` | 429 | — | `wait_and_retry` | Demasiadas solicitudes. Inténtalo de nuevo más tarde. |
| `error.500.internal` | 500 | — | `contact_support` | Algo salió mal |
| `error.503.auth_unavailable` | 503 | — | `wait_and_retry` | El servicio de autenticación no está disponible; inténtalo de nuevo más tarde |
| `error.503.profiles_not_configured` | 503 | — | `contact_support` | Este despliegue no tiene configurado un servicio de perfiles, por lo que aquí no se puede escribir un nombre para mostrar |
| `error.503.profiles_unavailable` | 503 | — | `wait_and_retry` | El servicio de perfiles no está disponible; inténtalo de nuevo más tarde |
