#pragma once
#include <Arduino.h>

// How much room is left in the NVS partition, counted in ENTRIES of 32 bytes.
// `available` is the honest figure: it excludes the page NVS keeps in hand so
// that it can garbage-collect. Returns false if the stats could not be read.
bool nvsEntryStats(uint32_t *used, uint32_t *available, uint32_t *total);

// Close enough to full that the next save may start failing -- and that the
// next BOOT may not find a free page at all. See nvsinfo.cpp for why that is
// worse than it sounds.
bool nvsLowOnSpace();

// One line for the serial log, plus a warning if it is running out. `when` is a
// label ("boot", "health") so a soak test can see the trend.
void nvsReport(const char *when);

// Preferences::put*() only ever says "short or not" -- it never surfaces the
// real esp_err_t underneath. This does one raw, throwaway nvs_set_u8()+commit()
// against the SAME "tamapoke" namespace, using a DIFFERENT key ("nvsprobe")
// from whatever just failed. A clean "ESP_OK" here only proves the namespace
// itself still accepts writes -- it says nothing about the specific key that
// failed, which can be individually wedged (see nvsProbeKey() below) while
// the rest of the namespace is fine. Kept as the cheap first check.
const char *nvsProbeWrite();

// The more useful probe: nvs_erase_key() on the EXACT key that just failed,
// then commit, reporting what the erase itself returned:
//   ESP_OK               -- an entry existed under this key and was removed.
//                            If a retry of the original write succeeds after
//                            this, that entry -- not the namespace -- was the
//                            problem (a classic cause: the key was ever
//                            written as a different NVS value type, which
//                            silently refuses every write of the new type
//                            until the old entry is erased).
//   ESP_ERR_NVS_NOT_FOUND -- no entry exists under this key at all, so a
//                            stale/mismatched entry cannot be why the write
//                            just failed; look elsewhere.
//   anything else         -- that key's slot is broken some other way.
// Real erasure, not a dry run -- only call this right after logging a
// failure you are about to retry anyway.
const char *nvsProbeKey(const char *key);

// One log line for ANY failed put*() -- not just the checkpoints. `context` is
// the caller ("pet", "player", "party", "bag") and `key` is the NVS key that
// just returned 0/short. Every legacy key write used to be silent (see
// nvsinfo.cpp's header comment); this is what closes that gap. Runs both
// probes above and logs both results.
void logKeyFailure(const char *context, const char *key);

// The most direct probe of all: nvs_set_blob() with the EXACT key, data and
// length that Preferences::putBytes() just refused, called raw. Neither
// nvsProbeWrite() (a different key) nor nvsProbeKey() (an erase, not a write)
// can produce the real esp_err_t for THIS specific write -- this is the one
// that can. Only call it right after a checkpoint write has already failed;
// it duplicates that write, not a dry run.
const char *nvsProbeBlobWrite(const char *key, const void *data, size_t len);
