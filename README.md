# HidrogenCellEnergy
Dashboard web para controlar un sistema de celula de hidrogeno con monitoreo, alarmas y simulacion HIL.

## Requisitos

- Python 3.11+.
- `pip` para instalar dependencias.
- No se requiere conexion a internet para cargar la interfaz una vez instaladas las dependencias; Bootstrap, Socket.IO client y Chart.js se sirven desde `static/vendor`.

## Instalacion rapida

1. Crea y activa un entorno virtual:

```bash
python -m venv venv
source venv/bin/activate
```

2. Instala dependencias:

```bash
pip install -r requirements.txt
```

## Variables de entorno necesarias

Antes de arrancar la aplicacion en produccion, define al menos esta variable:

```bash
export SCADA_SECRET_KEY="clave-secreta-y-segura"
```

- `SCADA_SECRET_KEY`: clave de sesion Flask. Obligatoria en produccion para preservar la integridad de las sesiones.

## Primer arranque

1. Genera los certificados para la encriptacion y que la informacion no viaje en texto plano
```bash
cd certificados
./generar.sh
```
2. Ejecuta la aplicacion:

```bash
python app.py
```

3. Abre el navegador en:

```text
https://localhost:5000
```

4. Si la base de datos es nueva la aplicacion mostrara un `setup` para crear el primer administrador con el usuario y contraseña que elijas. Se recomienda que el usuario no se llame "Admin/admin" por motivos de seguridad.

> Nota: `app.py` usa `certificados/cert.pem` y `certificados/key.pem` para HTTPS. Si no usas estos certificados, generalos con el script que esta en la carpeta de certificados. Esto en caso de haber saltado dicho paso

## Uso basico

- Inicia sesion.
- Si la base de datos es nueva, la aplicacion crea `data/horno.db` y solicita crear el primer administrador en `/setup`.
- El panel principal muestra el estado del sistema, alarmas y registros.
- Desde el area de administracion puedes cambiar configuracion, usuarios y revisar logs.

## Simulacion HIL

- El proyecto incluye un modo de simulacion en `controller.py`.
- El simulador HIL esta en `HIL.py` y emula un servidor Modbus TCP local. Esto idealmente ejecutarlo en una Raspberry pi o placas similares u otro equipo, esto con el fin de simular un Hardware in the Loop lo mas robusto y real posible.

## Problemas comunes

- Si la aplicacion no crea el admin inicial, comprueba que la tabla de usuarios este vacia en `data/horno.db`. O borrarla ya que si no existe se creara.
- Si no puedes iniciar sesion, comprueba la base de datos en `data/horno.db`.
- Si la aplicacion no arranca por HTTPS, asegurate de que los certificados existen en la carpeta `certificados`.
- Si necesitas cambiar la configuración Modbus, detén el sistema antes de actualizar los parámetros. Esto solo aplica para Hardware in the Loop (HIL).
