# Prueba de navegador aislado en VM — 2026-10-01

## Resultado

El navegador no resuelve actualmente el acceso documental desde esta VM.

| Prueba | Resultado |
| --- | --- |
| API ficha1242359 por HTTP (prueba anterior) | 200, ocho etapas, una base administrativa |
| Chromium headless, navegación al portal PROD4 | 403 antes de renderizar la SPA |
| Chromium headed con Xvfb, portal PROD4 | 403, ningún control visible |
| Chromium headed, enlace público conocido de las bases | 403, ningún PDF descargado |

La respuesta en ambas navegaciones headed fue:

```json
{"code":"403","message":"Forbidden","RequestId":"${http.request.id}"}
```

No hubo formulario de acceso ni CAPTCHA. El formato coincide con un ejemplo
de acción de [Oracle WAF](https://docs.oracle.com/en-us/iaas/Content/WAF/Actions/create_action.htm),
pero esto no identifica de manera concluyente el proveedor o la regla de bloqueo.
No se concluye que todas las IP de nube estén bloqueadas ni que una contraseña
resuelva el problema. La ficha visual no llegó a cargarse: **no se pudo probar
su botón**; sí se probó el enlace oficial observado en sus metadatos.

## Entorno y alcance

- VM ARM64 actual, misma conexión de salida que el worker.
- Imagen oficial `mcr.microsoft.com/playwright/python:v1.60.0-noble`.
- Digest `sha256:8ff591d613b01c884cc488339ed4318b4513eaf0c57a164a878ba49e70e3f384`.
- Playwright1.60, Chromium, usuario no root `pwuser`, sandbox habilitado y
  perfil seccomp oficial de la misma versión; TLS validado.
- Contenedor separado, 2GiB RAM, una CPU, 256MiB shm, sin puertos publicados,
  credenciales, Docker socket, red de producción, PostgreSQL ni Gemini.
- Sin proxies, rotación de IP, evasión de CAPTCHA, técnicas stealth o flags
  para ignorar certificados.
- No se reinició ni cambió ningún servicio de BuenaPro.

Script reproducible: `scripts/probe_prod4_browser.py`. Para diagnóstico local
inspecciona primero el DOM; `--download-text` solo puede ser un control real
previamente observado. Un timeout de descarga no demuestra por sí mismo un
403: en esta prueba la respuesta HTTP sí se registró expresamente como403.

Enlace observado: expediente1242359, documento
`3e125876-d6bb-4c68-be6e-86dc61c00f2e`, `CP142026__Bases Suscripciones PAM.pdf`.

## Evidencia

Reportes y capturas: `docs/sources/evidence/prod4-vm-browser-20261001/out/`.
La carpeta de evidencia remota es
`/home/ubuntu/prod4-browser-probe.GTV5Tj/out/`.

El contenedor y la imagen de prueba se retiraron al terminar; los reportes,
capturas y script se conservan. No se descargaron bases ni se procesaron
concursos con LLM. El refresco automático de cronogramas no fue alterado.
