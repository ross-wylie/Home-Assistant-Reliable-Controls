# Changelog

## 1.5.3

- Restore `log_version`, `log_inventory` and `_warn_same_board`, accidentally
  removed while rewriting the purge routine
- Add a startup smoke test that exercises `Bridge.run()` against the mock panel,
  so a missing method fails in CI rather than on a building

## 1.5.2

- **Purge now matches the whole `reliable_` namespace** instead of only
  currently-configured panels. Orphans are by definition from panels no longer
  configured, so the previous scoping skipped exactly the set it was meant to
  clean.
- Purge collects until the broker goes quiet rather than for a fixed 6 seconds;
  thousands of retained configs do not arrive that fast
- Unsubscribe before restoring the message handler. The old order funnelled
  retained discovery configs into the command handler, one log line each.

## 1.5.1

- Print a version banner with file hashes at startup, so it is unambiguous
  whether the container was actually rebuilt

## 1.5.0

- Two poll schedules: `fast_points` / `fast_interval` for a handful of points,
  `poll_interval` for the full sweep

## 1.4.1

- Optimistic echo on write, so the UI holds the new value instead of snapping
  back while the read-back completes
- Worker thread wakes immediately on a command rather than on a 0.5s tick

## 1.4.0

- **Writes are queued, never performed on the MQTT callback thread.** Blocking
  paho's network thread on the poll lock stalled the whole client: no state
  publishes, and keepalive expiry could drop the connection.
- Poll lock is released between banks, and states publish progressively rather
  than in one burst at the end of a sweep

## 1.3.2

- Device names come from the configured panel name, so two entries pointing at
  one physical board are visibly distinct
- Warn when two panels return identical points and the same panel name

## 1.3.1

- `purge_on_start` to clear orphaned retained MQTT discovery configs

## 1.3.0

- `split_subnets`: each SubLAN controller becomes its own HA device, nested
  under the main panel. `unique_id` is unchanged, so existing entities are
  re-parented rather than duplicated.

## 1.2.2

- Discard sub-controllers that merely echo the main controller's points

## 1.2.0

- `subnet_mode` (bitmap / probe / auto) and a `subnets` diagnostic command
- **Accept the sub-controller reply under command 150 as well as 108.** The
  original driver maps both; accepting only 108 made sub-boards invisible.

## 1.1.0

- Multiple panels in one instance, one thread and socket each
- Time-based availability instead of consecutive-failure counting

## 1.0.1

- Add `build.yaml`. Supervisor 2026.04.0 removed the automatic `BUILD_FROM`
  fallback, so the build failed without it.

## 1.0.0

- Initial release
