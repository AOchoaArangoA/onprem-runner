import sys
import time

time.sleep(1)
print("Consolidando hojas de cálculo...")
print("ERROR: no se pudo abrir \\srv-fin\reportes\mensual.xlsx (acceso denegado)", file=sys.stderr)
sys.exit(2)
