# Clips to film at the printer

Four clips in [GETTING-STARTED.md](../GETTING-STARTED.md) show the printer itself. Each is short
(5 to 15 seconds), filmed on a phone held still, landscape, in good light. No narration: the guide
carries the words, and a caption is added afterwards.

| File | Step | What to film | Length |
|---|---|---|---|
| `01-find-ip.mp4` | 1 | The **stock** touchscreen: tap Settings, then Network, hold on the IP address for 3 seconds | 10 s |
| `02-usb-stick.mp4` | 2 | Close-up of the front USB port; the stick goes in; hold 2 seconds | 5 s |
| `06-pairing-screen.mp4` | 6 | The touchscreen after the first Eitri boot, showing the pairing code; hold 3 seconds | 5 s |
| `08-probe-calibrate.mp4` | 8 | Side view at bed height: paper under the nozzle, the nozzle lowered in steps, the paper slid until it drags | 15 s |

Before filming:

- **01** needs the stock firmware, so film it before an install (or after `revert.sh --hard`).
- **06** needs a printer that has not been paired: `sudo eitri-configurator unlock` reopens pairing.
- Wipe the bed and nozzle; nothing personal in view (network name, labels with serial numbers).
  Crop or blur the IP in 01 if it is not a private `192.168.x.x` / `10.x.x.x` address.

After filming, from the repository root:

```sh
# trim, scale to 960 wide, no sound; then a GIF for the guide
ffmpeg -ss 0 -t 10 -i raw.mov -an -vf "scale=960:-2,fps=12" -c:v libx264 -crf 26 -pix_fmt yuv420p docs/tutorial/01-find-ip.mp4
ffmpeg -i docs/tutorial/01-find-ip.mp4 -vf "fps=8,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse" docs/tutorial/01-find-ip.gif
```

Then replace the clip's *to be filmed* line in GETTING-STARTED.md with an image line for `tutorial/01-find-ip.gif`.

## The other clips

- `03-login`, `04-dry-run`, `05-install`: terminal clips rendered by `tools/tutorial/termclip.py` from
  the tapes in `tapes/`. Each tape names the log its lines come from. Re-record from a real
  printer at the first release.
- `06-pair`, `07-wizard-detect`, `07-wizard-options`, `08-zoffset`, `09-done`: the real setup wizard,
  recorded by `configurator/tools/tutorial_clips.py` against its mock printer (one continuous run, cut
  into a clip per step).
