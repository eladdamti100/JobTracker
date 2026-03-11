/**
 * JobTracker — WhatsApp Group Job Listener
 *
 * Connects to WhatsApp, listens to configured job groups,
 * extracts URLs from messages, and forwards them to the Python bridge.
 *
 * Run:  node scanners/whatsapp_group.js
 * First run: scan the QR code with WhatsApp on your phone.
 * Session is saved to whatsapp_session/ — no QR needed on subsequent runs.
 */

require("dotenv").config({ path: require("path").join(__dirname, "..", ".env") });

const { Client, LocalAuth } = require("whatsapp-web.js");
const qrcode = require("qrcode-terminal");
const axios = require("axios");
const fs = require("fs");
const path = require("path");

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const BRIDGE_URL = process.env.BRIDGE_URL || "http://127.0.0.1:5001";
const LOG_FILE = path.join(__dirname, "..", "logs", "whatsapp.log");
const SESSION_DIR = path.join(__dirname, "..", "whatsapp_session");
const HOURS_24_MS = 24 * 60 * 60 * 1000;

// Target group invite codes (last segment of chat.whatsapp.com/... links)
const TARGET_INVITE_CODES = [
  "GyDcchhpJez9EUXebW6SHc", // Referally Student — posts LinkedIn/external job links
];

// Set of chat IDs we are listening to (populated on ready)
const targetChatIds = new Set();

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------

fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });

function log(msg, level = "INFO") {
  const line = `[${new Date().toISOString()}] [${level}] ${msg}`;
  console.log(line);
  try {
    fs.appendFileSync(LOG_FILE, line + "\n");
  } catch (_) {}
}

// ---------------------------------------------------------------------------
// URL extraction
// ---------------------------------------------------------------------------

const URL_REGEX = /https?:\/\/[^\s<>"{}|\\^`[\]]+/g;

// Domains / path patterns to SKIP (not actual job listings)
const SKIP_DOMAINS = [
  "chat.whatsapp.com",
  "wa.me",
  "maps.google",
  "goo.gl/maps",
  "youtu.be",
  "youtube.com",
  "t.me",
  "linktr.ee",
  "instagram.com",
  "facebook.com",
  "twitter.com",
  "x.com",
  "tiktok.com",
];

// URL paths to skip (homepages, search pages — not individual job listings)
const SKIP_PATHS = [
  /hiremetech\.com\/jobs-app/,       // hiremetech search/homepage
  /hiremetech\.com\/?$/,             // hiremetech root
  /hiremetech\.com\/jobs\/?$/,       // hiremetech jobs listing page
];

function extractJobUrls(text) {
  const matches = text.match(URL_REGEX) || [];
  return [...new Set(matches)].filter((url) => {
    // Skip blacklisted domains
    if (SKIP_DOMAINS.some((d) => url.includes(d))) return false;
    // Skip non-job paths (homepages, search pages)
    if (SKIP_PATHS.some((re) => re.test(url))) return false;
    return true;
  });
}

// ---------------------------------------------------------------------------
// Bridge calls
// ---------------------------------------------------------------------------

async function isUrlInDb(url) {
  try {
    const res = await axios.get(`${BRIDGE_URL}/check_url`, {
      params: { url },
      timeout: 5000,
    });
    return res.data.exists === true;
  } catch {
    return false; // assume not in DB if bridge is unreachable
  }
}

async function sendToBridge(url, groupName) {
  try {
    const res = await axios.post(
      `${BRIDGE_URL}/new_job`,
      { url, group_name: groupName },
      { timeout: 10000 }
    );
    log(`Bridge accepted: ${url} → status=${res.data.status}`);
    return true;
  } catch (e) {
    log(`Bridge error for ${url}: ${e.message}`, "ERROR");
    return false;
  }
}

// ---------------------------------------------------------------------------
// Group discovery — direct lookup by invite code (no getChats() needed)
// ---------------------------------------------------------------------------

async function discoverTargetGroups(client) {
  log("Discovering target groups via invite codes...");

  for (const code of TARGET_INVITE_CODES) {
    try {
      const info = await client.getInviteInfo(code);
      const groupId = info.id._serialized;
      targetChatIds.add(groupId);
      log(`Resolved invite "${code}" → "${info.subject}" (${groupId})`, "SUCCESS");
    } catch (e) {
      log(`Could not resolve invite code "${code}": ${e.message}`, "WARN");
    }
  }

  if (targetChatIds.size === 0) {
    log("No target groups resolved — check that you are a member of the groups.", "WARN");
  } else {
    log(`Listening to ${targetChatIds.size} group(s) ✓`, "SUCCESS");
  }
}

// ---------------------------------------------------------------------------
// Message processing
// ---------------------------------------------------------------------------

async function processMessage(msg, chatName) {
  // 24-hour filter
  const ageMs = Date.now() - msg.timestamp * 1000;
  if (ageMs > HOURS_24_MS) return;

  const body = msg.body || "";
  const urls = extractJobUrls(body);
  if (urls.length === 0) return;

  log(`Message in "${chatName}" — found ${urls.length} URL(s)`);

  for (const url of urls) {
    if (await isUrlInDb(url)) {
      log(`Already in DB, skipping: ${url}`);
      continue;
    }
    log(`Forwarding to bridge: ${url}`);
    await sendToBridge(url, chatName);
  }
}

async function catchUpRecentMessages(client) {
  log("Catching up on last 24h messages in target groups...");
  for (const chatId of targetChatIds) {
    try {
      const chat = await client.getChatById(chatId);
      const messages = await chat.fetchMessages({ limit: 100 });
      log(`"${chat.name}": scanning ${messages.length} recent messages`);

      let forwarded = 0;
      for (const msg of messages) {
        const ageMs = Date.now() - msg.timestamp * 1000;
        if (ageMs <= HOURS_24_MS && msg.body) {
          const urls = extractJobUrls(msg.body);
          for (const url of urls) {
            if (!(await isUrlInDb(url))) {
              await sendToBridge(url, chat.name);
              forwarded++;
            }
          }
        }
      }
      log(`"${chat.name}": forwarded ${forwarded} new URL(s) from history`);
    } catch (e) {
      log(`Error scanning "${chatId}": ${e.message}`, "ERROR");
    }
  }
}

// ---------------------------------------------------------------------------
// WhatsApp client
// ---------------------------------------------------------------------------

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: SESSION_DIR }),
  puppeteer: {
    headless: true,
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--disable-dev-shm-usage",
      "--disable-gpu",
    ],
  },
});

client.on("qr", (qr) => {
  log("QR code — scan with WhatsApp (Settings → Linked Devices → Link a Device):");
  qrcode.generate(qr, { small: true });
});

client.on("authenticated", () => {
  log("WhatsApp authenticated ✓", "SUCCESS");
});

client.on("auth_failure", (msg) => {
  log(`Authentication failed: ${msg}`, "ERROR");
  process.exit(1);
});

client.on("ready", async () => {
  log("WhatsApp client ready ✓", "SUCCESS");
  await discoverTargetGroups(client);
  await catchUpRecentMessages(client);
  log("Listening for new messages...");
});

// Real-time incoming messages
client.on("message", async (msg) => {
  try {
    // Only group messages
    if (!msg.from.endsWith("@g.us")) return;
    // Only our target groups
    if (!targetChatIds.has(msg.from)) return;

    const chat = await msg.getChat();
    await processMessage(msg, chat.name);
  } catch (e) {
    log(`message handler error: ${e.message}`, "ERROR");
  }
});

// Auto-reconnect on disconnect
client.on("disconnected", (reason) => {
  log(`Disconnected: ${reason} — reconnecting in 10s...`, "WARN");
  setTimeout(() => {
    log("Re-initializing client...");
    client.initialize().catch((e) =>
      log(`Re-init error: ${e.message}`, "ERROR")
    );
  }, 10000);
});

log("Starting WhatsApp listener...");
client.initialize().catch((e) => {
  log(`Init error: ${e.message}`, "ERROR");
  process.exit(1);
});
