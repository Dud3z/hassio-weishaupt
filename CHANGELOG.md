# Changelog

## Unreleased

- Improvement: Pause all requests after the device fails to answer properly (empty reply, `CMD_ERROR` on every register, timeout or unreachable): 2, 4, 8, 16, then at most 30 minutes, reset by the first healthy reply. A failed request also ends the current poll instead of sending the remaining batches right away. Entities are unavailable during the pause and the next poll waits for it to end. Immediate retries have locked the WEM's JSON interface until a power cycle (findings by @jvejmelka in [weishaupt-wem-can-esphome](https://github.com/jvejmelka/weishaupt-wem-can-esphome)).
- Improvement: Allow at most one write per minute (Betriebsart select, Warmwasser switch, Warmwasser-Push button). A write refused by this limit or during a pause sends nothing and shows an error in Home Assistant instead of failing silently.
- Improvement: Read each register no more often than its update class in Weishaupt's register table (30 s, 60 s or 10 min), and do not read registers behind disabled entities. With the default entities and no expansion modules this lowers the requests to the device from about 1200 to about 670 per hour. A register written by a select or switch is read back right away.
- Feature: A repair issue appears when the device has not answered for 30 minutes, listing the known causes (WEM Portal enabled, a second client, lock-up until power cycle); it clears itself on recovery.
- Feature: *Configure* changes the scan interval after setup.
- Feature: Repair issues while the Systemgerät reports an error (with message type and error number), when its clock is more than 5 minutes off Home Assistant's time in two checks in a row (e.g. after the DST change or a power cut; the time is read every 10 minutes for this, even with the `Uhrzeit / Datum` entity disabled), and when the maintenance is due.
- Feature: Maintenance reminder: set the interval in months and the last maintenance under *Configure*; a `Nächste Wartung` sensor shows the due date and a `Wartung erledigt` button records today.
- Feature: *Reconfigure* can change the address of the Systemgerät; the new address is checked with the stored credentials, and entities, history and automations are kept. Re-detecting the optional modules is now a checkbox there (off by default), so changing the address sends no probes.
- Feature: Home Assistant offers the setup when a Systemgerät with the DHCP hostname `wem-sg*` joins the network, unless it is already configured by IP or hostname.
- Feature: `Abfragen pausieren` switch stops every request to the device (reads and writes), for example while the WEM app, the portal or a technician uses it; the WEM handles only one client at a time. The last values stay, the connection sensor shows `suspended`, and the setting survives a restart.
- Improvement: Icons follow the state: burner phase (flame off/on, fan while purging), Warmwasser pump, fault status, the Warmwasser switch and the connection sensor.
- Improvement: The seven cascade sensors (Folgewechsel, Wechselzeit, Kaskade …) start disabled on new installations, so systems without an EM-KA cascade module neither show nor poll them. Existing installations keep them; disable them in the entity settings if you have no cascade.
- Feature: Re-authenticate when the device rejects the stored credentials (HTTP 401) instead of failing every poll.
- Feature: `Störung` binary sensor (problem) from the existing error block, with message type and error number as attributes.
- Feature: Diagnostic `Verbindung` sensor showing `ok`, `paused` (with `paused_until`) or `error`.
- Feature: Download diagnostics with raw register values, detected modules and connection state; host and credentials are redacted.
- Maintenance: Keep the coordinator in `entry.runtime_data`; remove the unsupported `platforms` key from `manifest.json` and add `issue_tracker`; remove the invalid `type` key from `hacs.json`; add a GitHub Actions workflow running the unit tests, hassfest and the HACS validation.
- Fix: The test suite now also passes when all test modules run together (`python -m unittest discover -s tests`); the Home Assistant stand-ins live in one shared `tests/ha_stubs.py`.

## 0.3.2 - 2026-11-01

- Fix: Swap the register addresses of the "WTC Abgastemperatur" (now `OX=0x2537`, `OS=0x00`) and "WTC Rücklauftemperatur" (now `OX=0x2533`, `OS=0x02`) sensors. The values were reversed compared to the boiler's display (#18, thanks @cg089). The Modbus register references are unchanged.

## 0.3.1 - 2026-10-21

- Upgrade: Users upgrading from a version before 0.3.0 should remove and reinstall the integration, so legacy unavailable optional entities and devices are cleared from Home Assistant's registry. Users already on 0.3.0 can simply update.
- Feature: Add a "WTC Istleistung" sensor exposing the boiler's current modulation level in percent (CanApiJson `MI=07`, `OX=0x2534`, raw / 100, rounded to whole percent to match the display). Identified and confirmed on a WTC-GB 15-B Kompakt; not part of the official Modbus register table. Thanks @Dud3z for finding the register (BorgNumberOne/Weishaupt_CanApiJson#7) and for the implementation (#16).
- Feature: Add a `Betriebsart Warmwasser` switch (DHW on/off) for the CanApiJson object `MI 03 / OX 0x2520 / OS 02` (1 = Ein, 2 = Aus). The object is not in Weishaupt's official register table; the address was verified on a WTC-GW 15-B by @jvejmelka (#4).
- Fix: Store the optional-module detection (Heizkreis 2, Heizkreis 3, Solar) in the config entry. Restarts and reloads no longer send probes to absent modules, which could lock the Systemgerät after a few quick setups (#15, thanks @zebbl for the reports and measurements). Only complete probe answers are stored; a probe without any answer is retried on the next setup.
- Feature: Add *Reconfigure → Re-detect optional modules* to probe again after adding or removing a module. Reloading the integration no longer re-detects; use this instead.

## 0.3.0 - 2026-09-16

- Fix: Replace the deprecated `via_device` parameter with `via_device_id` when linking sensor, select and button devices to the Systemgerät, avoiding a deprecation warning that would break in HA Core 2027.8.0 (#14).
- Improvement: Reduce the CanApiJson read batch size from 10 to 6 VG frames and enforce a minimum ~300ms gap between requests, avoiding intermittent `CMD_ERROR` responses observed on some hardware with larger/faster batches.
- Improvement: Auto-detect optional device groups (Heizkreis 2, Heizkreis 3, Solar) from the first successful poll instead of always creating them; installations without those expansion modules no longer get permanently-unavailable entities. Detection happens once per config entry load; reload the integration to re-probe.
- Credit: The batching, throttling and module-detection changes above are informed by real-hardware findings from [JS-DE-Tech/hacs-weishaupt-lan](https://github.com/JS-DE-Tech/hacs-weishaupt-lan), a fork of this integration, which also independently confirms the Heizkreis 3 `MX=2` assumption from #11.
- Fix: Probe optional device groups (Heizkreis 2, Heizkreis 3, Solar) once each during initial detection instead of polling every sensor in every absent group; this avoids permanently locking Systemgerät firmware that retains failed absent-module requests (#15).
- Documentation: Recommend removing and reinstalling the integration for users upgrading from before 0.3.0, so legacy unavailable optional entities and devices are cleared from Home Assistant's registry.

## 0.2.7 - 2026-09-02

- Feature: Add a "Heizkreis 3" sensor group for a second EM-HK expansion module, assuming it addresses itself as `MX=2` on the bus. Unconfirmed against real hardware; entities stay unavailable if the assumption doesn't match the device (#11).

## 0.2.6 - 2026-08-29

- Feature: Translate all sensor, select and button entity names via Home Assistant's `translation_key` mechanism instead of hardcoded German strings. Adds English and Italian translations alongside the existing German ones; any other HA language falls back to English (#12).
- Fix: Identify the existing EM-HK sensor group as Heizkreis 2 and document its `MX=1` register mapping (#11).
- Fix: Correct the swapped day/year registers and expose the device time as a timezone-aware timestamp (#10).

## 0.2.5 - 2026-04-19

- Fix: Make device timestamp timezone-aware (UTC) to fix auto-update of sensor values (#5, thanks @wagnerpizza).

## 0.2.3 - 2026-04-04

- Fix: Add missing `device_info` to `WeishauptSelectEntity` so the Select is visible in the HA device view.
- Feature: Add Button platform with `Warmwasser-Push` button (Register 131) that writes value 1 to trigger a domestic hot water push cycle.

## 0.2.2 - 2026-03-30

- Feature: Add writable Select for `sg_betriebsart_hk1_vorgabe` (Register 100) to allow changing Heizkreis 1 Betriebsart (#4).
- Feature: Add API write support for CanApiJson SET frames so writable registers can be updated from HA.

## 0.2.1 - 2026-03-21

- Fix: Create the SG system device explicitly so child devices reference a valid `via_device` parent and avoid upcoming Home Assistant registry breakage.
- Test: Add regression coverage for the SG root device and child device hierarchy.

## 0.2.0 - 2026-03-21

- Feature: Re-add the documented HK and SOL sensor groups from the PDF register map, restoring Heizkreis and Solar entities.
- Feature: Add the documented SG fault block diagnostics from Modbus 120-123 as disabled-by-default diagnostic sensors.
- Fix: Restore the documented SG time/date component registers so the consolidated `sg_device_time` sensor is populated correctly.
- Fix: Poll only real device frames and derive synthetic sensors from the fetched source frames.
- Fix: Guard against empty device responses and log a warning instead of failing with a `NoneType` error during coordinator refresh (#2).

## 0.1.3 - 2026-02-27

- Feature: Added logo.

## 0.1.2 - 2026-02-27

- Fix: sensor amount in README.md was outdated after removing HK and SOL device groups. Updated to reflect current sensor count (50).
- Fix: update manifest version to 0.1.2 for latest release.

## 0.1.1 - 2026-02-27

- Breaking: Drop `HK_SENSORS` and `SOL_SENSORS` from the integration (these device groups will no longer be registered by default). This reduces entity count and focuses the integration on `SG` and `WTC` device groups.

## 0.0.8 - 2026-02-27

- Release: prepare and publish v0.0.8 (includes latest fixes).

## 0.0.7 - 2026-02-27

- Reverted the parent device change from 0.0.5.

## 0.0.6 - 2026-02-27

- Release: prepare and publish v0.0.6 (bugfixes and registry fixes).

## 0.0.5 - 2026-02-27

- Fix: create parent device so `via_device` references are valid and avoid registry warnings.


## 0.0.4 - 2026-02-27

- Fix: Treat sentinel raw values (0x8000/0xFFFF) as unavailable so sensors don't report extreme negative temperatures.

## 0.0.3 - 2026-02-27

- Consolidate WEM Systemgerät diagnostic time/date registers into a single `Uhrzeit / Datum` (`sg_device_time`) timestamp sensor.
- Remove the separate `Uhrzeit (Stunden)`, `Uhrzeit (Minuten)`, `Datum (Tag)`, `Datum (Monat)`, and `Datum (Jahr)` sensors.
- Diagnostic sensors remain disabled by default.

## 0.0.2

- Previous changes.
