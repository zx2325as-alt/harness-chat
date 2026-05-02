/**
 * 会话列表持久化：优先 IndexedDB（容量大），不可用时回退 localStorage。
 */

const DB_NAME = "harness_chat_sessions_v1";
const DB_VER = 1;
const STORE = "kv";
const KEY_SESSIONS = "sessions_json";

function openDb() {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === "undefined") {
      reject(new Error("indexedDB_unavailable"));
      return;
    }
    const req = indexedDB.open(DB_NAME, DB_VER);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE);
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error || new Error("idb_open_failed"));
  });
}

export async function idbSessionsSupported() {
  try {
    await openDb();
    return true;
  } catch {
    return false;
  }
}

/** @returns {Promise<string|null>} JSON 字符串或 null */
export async function loadSessionsJsonFromIdb() {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readonly");
    const req = tx.objectStore(STORE).get(KEY_SESSIONS);
    req.onsuccess = () => {
      const v = req.result;
      resolve(v != null ? String(v) : null);
    };
    req.onerror = () => reject(req.error);
  });
}

/** @param {string} json */
export async function saveSessionsJsonToIdb(json) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).put(json, KEY_SESSIONS);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error("idb_tx_abort"));
  });
}

export async function clearSessionsIdb() {
  try {
    const db = await openDb();
    await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).delete(KEY_SESSIONS);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  } catch {
    /* ignore */
  }
}
