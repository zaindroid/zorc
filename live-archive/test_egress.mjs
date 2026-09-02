import { EgressClient, RoomServiceClient, EncodedFileOutput, EncodedFileType, S3Upload } from 'livekit-server-sdk';
import fs from 'fs';

function parseIni(text) {
  const out = {};
  let section = null;
  for (const line of text.split('\n')) {
    const t = line.trim();
    if (!t || t.startsWith('#') || t.startsWith(';')) continue;
    const sec = t.match(/^\[(.+)\]$/);
    if (sec) { section = sec[1]; out[section] = {}; continue; }
    const kv = t.match(/^([^=]+)=(.*)$/);
    if (kv && section) out[section][kv[1].trim()] = kv[2].trim();
  }
  return out;
}

const lk = JSON.parse(fs.readFileSync('/home/zman/zorc/live-archive/secrets/livekit.json', 'utf8'));
const rclone = parseIni(fs.readFileSync('/home/zman/zorc/backup/secrets/rclone.conf', 'utf8'));
const r2 = rclone['r2'];
const bucket = 'servingz-backups';

const roomService = new RoomServiceClient(lk.url, lk.api_key, lk.api_secret);
const egressClient = new EgressClient(lk.url, lk.api_key, lk.api_secret);

const roomName = `egress-test-${Date.now()}`;

async function main() {
  console.log('Creating test room:', roomName);
  await roomService.createRoom({ name: roomName, emptyTimeout: 60 });

  const key = `iht-news-live/test-egress-${Date.now()}.mp4`;
  const output = new EncodedFileOutput({
    fileType: EncodedFileType.MP4,
    filepath: key,
    output: {
      case: 's3',
      value: new S3Upload({
        accessKey: r2.access_key_id,
        secret: r2.secret_access_key,
        bucket,
        endpoint: r2.endpoint,
        region: 'auto',
        forcePathStyle: true,
      }),
    },
  });

  console.log('Starting RoomCompositeEgress with S3 output ->', bucket, key);
  let info;
  try {
    info = await egressClient.startRoomCompositeEgress(roomName, { file: output });
    console.log('Egress started OK. egressId:', info.egressId, 'status:', info.status);
  } catch (e) {
    console.log('EGRESS START FAILED:', e.message || e);
    await roomService.deleteRoom(roomName).catch(() => {});
    process.exit(1);
  }

  await new Promise((r) => setTimeout(r, 8000));

  try {
    await egressClient.stopEgress(info.egressId);
    console.log('Egress stop requested.');
  } catch (e) {
    console.log('Stop failed (may have already finished):', e.message || e);
  }

  await new Promise((r) => setTimeout(r, 5000));

  const results = await egressClient.listEgress({ roomName });
  for (const r of results) {
    console.log('--- Egress result ---');
    console.log('status:', r.status);
    console.log('error:', r.error || '(none)');
    if (r.fileResults && r.fileResults.length) {
      for (const f of r.fileResults) {
        console.log('file:', f.filename, 'size:', f.size, 'duration:', f.duration);
      }
    }
  }

  await roomService.deleteRoom(roomName).catch(() => {});
  console.log('Test room cleaned up.');
}

main().catch((e) => {
  console.log('FATAL:', e.message || e);
  process.exit(1);
});
