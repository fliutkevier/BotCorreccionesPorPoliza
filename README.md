# Bot de Correcciones

Automatización web en Python que carga las correcciones de lecturas en el portal, reemplazando la carga manual póliza por póliza.

## Qué hace

Recibe el turno, las rutas y las pólizas a corregir, y opera el portal recorriendo tres fases secuenciales:

| Fase | Acción |
|------|--------|
| **A** | Carga el turno y presiona *actualizar* para traer el listado del período. |
| **B** | Selecciona las rutas correspondientes dentro del turno cargado. |
| **C** | Carga las pólizas y confirma la corrección. |

Cada fase espera a que el portal termine de responder antes de avanzar: el sitio muestra un overlay bloqueante (`gx mask`) mientras procesa, y el bot valida su aparición y desaparición antes de cada interacción. Esa validación es lo que evita que se saltee selecciones en fase B, a costa de un tiempo de ejecución mayor que el de la versión anterior.

## Requisitos

- Python 3.x
- Dependencias: `pip install -r requirements.txt`
- Driver del navegador compatible con la versión instalada
- Credenciales y URL del portal (configuradas fuera del repo)

## Uso

```bash
python portal.py
```

Datos de entrada: turno, rutas y listado de pólizas.

## Problemas conocidos

- **Recarga redundante en fase A**: después de cargar el turno el bot recarga la página sin necesidad y lo carga dos veces antes de presionar *actualizar*. No rompe el flujo, pero suma tiempo a cada ejecución.
- **Performance**: las validaciones contra el `gx mask` bajaron bastante la velocidad respecto de la versión previa. La versión vieja era más rápida pero omitía selecciones en fase B cuando el mask aparecía por un instante.

## Archivos

- `portal.py` — lógica completa del bot (fases A/B/C, esperas y validaciones).
