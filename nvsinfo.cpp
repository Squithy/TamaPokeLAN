// How much room is left in NVS, and why anybody should care.
//
// A save rewrites every key it owns, and NVS is log-structured: each put appends
// a fresh entry and marks the old one stale, reclaiming the space only by
// garbage-collecting a whole 4 KB page. The stock app3M_fat9M_16MB table gives
// `nvs` 20 KB -- five pages, roughly 630 entries of 32 bytes -- and NVS needs a
// free page in hand to compact into.
//
// If it ever runs out, nvs_flash_init() returns ESP_ERR_NVS_NO_FREE_PAGES, and
// the Arduino core's response (esp32-hal-misc.c, initArduino) is to ERASE THE
// ENTIRE PARTITION before setup() is ever reached:
//
//     err = nvs_flash_init();
//     if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
//       ... esp_partition_erase_range(partition, 0, partition->size);
//
// Every save on the device, gone, with nothing a player could ever see. The
// firmware could not see it coming either: putX() returning short was discarded
// at every call site except the checkpoints.
//
// This cannot prevent that. What it does is put the number in the log at boot
// and in the HEALTH heartbeat, so the trend is visible during a soak test rather
// than discovered by a wiped board. The real fix is fewer writes per save --
// see the checkpoint note in pet.cpp, and the ~50 legacy keys still written
// beside them.
//
// Not compiled into the emulator: its NVS is a std::map with no partition to
// exhaust, so host_impl.cpp stubs these. Same arrangement as rtcbat.cpp.
#include "nvsinfo.h"
#include <nvs.h>
#include <nvs_flash.h>

// Roughly one page's worth. NVS wants a spare page to compact into, so being
// inside a single page of the end is the point at which the next save is living
// on borrowed time.
#define NVS_LOW_ENTRIES 126

bool nvsEntryStats(uint32_t *used, uint32_t *available, uint32_t *total) {
  nvs_stats_t s;
  if (nvs_get_stats(NULL, &s) != ESP_OK) return false;
  if (used) *used = (uint32_t)s.used_entries;
  if (available) *available = (uint32_t)s.available_entries;
  if (total) *total = (uint32_t)s.total_entries;
  return true;
}

bool nvsLowOnSpace() {
  uint32_t avail = 0;
  if (!nvsEntryStats(nullptr, &avail, nullptr)) return false;
  return avail < NVS_LOW_ENTRIES;
}

void nvsReport(const char *when) {
  uint32_t used = 0, avail = 0, total = 0;
  if (!nvsEntryStats(&used, &avail, &total)) {
    Serial.printf("nvs %s: stats unavailable\n", when);
    return;
  }
  Serial.printf("nvs %s: used=%lu avail=%lu total=%lu\n", when,
                (unsigned long)used, (unsigned long)avail, (unsigned long)total);
  if (avail < NVS_LOW_ENTRIES)
    Serial.printf("nvs %s: LOW -- a full partition is erased WHOLE on the next "
                  "boot; EXPORT now\n", when);
}

// Raw, bypasses Preferences entirely. Opens the SAME namespace independently
// (nvs_open does not conflict with an already-open Preferences handle on the
// same namespace -- NVS handles are reference-counted, not exclusive), writes
// one throwaway byte, commits, and reports exactly which call broke. This is
// the only way to see the real error: Preferences::putBytes()/putUChar()/etc.
// all collapse every failure mode down to "returned 0".
const char *nvsProbeWrite() {
  nvs_handle_t h;
  esp_err_t err = nvs_open("tamapoke", NVS_READWRITE, &h);
  if (err != ESP_OK) return esp_err_to_name(err);
  err = nvs_set_u8(h, "nvsprobe", 1);
  if (err != ESP_OK) { nvs_close(h); return esp_err_to_name(err); }
  err = nvs_commit(h);
  nvs_close(h);
  return esp_err_to_name(err);
}

// erase_key on the EXACT key, not a stand-in -- see nvsinfo.h for why the
// generic nvsProbeWrite() above cannot tell a wedged KEY from a healthy
// namespace, and what each possible return here actually means.
const char *nvsProbeKey(const char *key) {
  nvs_handle_t h;
  esp_err_t err = nvs_open("tamapoke", NVS_READWRITE, &h);
  if (err != ESP_OK) return esp_err_to_name(err);
  err = nvs_erase_key(h, key);
  if (err != ESP_OK) { nvs_close(h); return esp_err_to_name(err); }
  err = nvs_commit(h);
  nvs_close(h);
  return esp_err_to_name(err);
}

// The direct one: retries the EXACT write with the EXACT data, raw, so the
// esp_err_t that comes back is the real reason -- not inferred from a probe
// on a different key or an erase that only proves the slot was reachable.
const char *nvsProbeBlobWrite(const char *key, const void *data, size_t len) {
  nvs_handle_t h;
  esp_err_t err = nvs_open("tamapoke", NVS_READWRITE, &h);
  if (err != ESP_OK) return esp_err_to_name(err);
  err = nvs_set_blob(h, key, data, len);
  if (err != ESP_OK) { nvs_close(h); return esp_err_to_name(err); }
  err = nvs_commit(h);
  nvs_close(h);
  return esp_err_to_name(err);
}

void logKeyFailure(const char *context, const char *key) {
  uint32_t used = 0, avail = 0, total = 0;
  bool haveStats = nvsEntryStats(&used, &avail, &total);
  const char *probe = nvsProbeWrite();
  const char *erase = nvsProbeKey(key);
  if (haveStats) {
    Serial.printf("save: %s key '%s' failed -- nvs used=%lu avail=%lu total=%lu probe=%s erase=%s\n",
                  context, key, (unsigned long)used, (unsigned long)avail,
                  (unsigned long)total, probe, erase);
  } else {
    Serial.printf("save: %s key '%s' failed -- nvs stats unavailable probe=%s erase=%s\n",
                  context, key, probe, erase);
  }
}
