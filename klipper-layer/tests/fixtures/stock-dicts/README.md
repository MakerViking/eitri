# Stock MCU data dictionaries (QIDI X-Plus 4)

The command dictionaries of the firmware a QIDI X-Plus 4 ships with (QIDI's Klipper
v0.12.0-65 build, 2024-03-01), extracted from the compressed copy inside each board's
flash. They let the batch tests run the Eitri config against exactly what an unflashed
printer answers, so protocol mismatches show up offline.

| File | Board | MCU | Version |
|---|---|---|---|
| toolhead-qidi-v0.12.dict  | toolhead, `[mcu]`     | stm32f103xe (GD32F103) | v0.12.0-65-g96ab9069-dirty-20240301_100458-mkspi |
| mainboard-qidi-v0.12.dict | mainboard, `[mcu U_1]` | stm32f401xc | v0.12.0-65-g96ab9069-dirty-20240301_100654-mkspi |

They describe GPL firmware built from QIDI's published Klipper tree and contain no
user data.
