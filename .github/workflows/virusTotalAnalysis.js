const fs = require('fs');
const crypto = require('crypto');
const { execSync } = require('child_process');

function sha256File(filePath) {
  const hash = crypto.createHash('sha256');
  const fileBuffer = fs.readFileSync(filePath);
  hash.update(fileBuffer);
  return hash.digest('hex');
}

function sleep(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function countAPIUsageAndWait({core}) {
  const limit = process.env.VT_API_LIMIT ? Number(process.env.VT_API_LIMIT) : Infinity;
  if (core._apiUsageCount === undefined) {
    core._apiUsageCount = 0;
  }
  if (core._apiUsageCount >= limit) {
    core.info('VirusTotal API usage limit reached');
    core.setFailed('VirusTotal API usage limit reached');
    throw new Error('VirusTotal API usage limit reached');
  }
  sleep(30 * 1000);
  core._apiUsageCount++;
}

function runCommand(command) {
  return execSync(command, {
    encoding: 'utf8',
    stdio: ['pipe', 'pipe', 'pipe'],
    maxBuffer: 1024 * 1024 * 1024,
  });
}

function submitFileForScan({core}, addonFile) {
  core.info(`Submitting file to VirusTotal: ${addonFile}`);
  try {
    runCommand(`vt scan file -k ${process.env.VT_API_KEY} "${addonFile}"`);
  } catch (error) {
    core.error(`VirusTotal submission failed: ${error.message}`);
    throw error;
  }
}

function queryVirusTotal({core}, sha) {
  countAPIUsageAndWait({core});
  try {
    const raw = runCommand(`vt file ${sha} -k ${process.env.VT_API_KEY} --format json`);
    return JSON.parse(raw);
  } catch (error) {
    core.info('VirusTotal file query failed, will retry after submission.');
    return null;
  }
}

function runAnalysis({core}, addonFile) {
  if (!process.env.VT_API_KEY) {
    core.setFailed('VT_API_KEY must be configured in repository secrets');
    throw new Error('Missing VT_API_KEY');
  }

  const sha = sha256File(addonFile);
  const vtScanUrl = `https://www.virustotal.com/gui/file/${sha}`;
  let vtData = null;

  for (let attempt = 0; attempt < 5; attempt++) {
    vtData = queryVirusTotal({core}, sha);
    if (vtData && Array.isArray(vtData) && vtData[0] && vtData[0].last_analysis_stats) {
      break;
    }
    submitFileForScan({core}, addonFile);
    sleep(30 * 1000);
  }

  if (!vtData || !Array.isArray(vtData) || !vtData[0] || !vtData[0].last_analysis_stats) {
    core.setFailed('Unable to retrieve VirusTotal analysis results');
    throw new Error('VirusTotal analysis results unavailable');
  }

  const stats = vtData[0].last_analysis_stats;
  const malicious = Number(stats.malicious || 0);
  const output = {
    vtScanUrl,
    vtResults: vtData,
  };

  fs.writeFileSync('vt-results.json', JSON.stringify(output, null, 2));
  core.setOutput('vtScanUrl', vtScanUrl);
  core.setOutput('vtResults', JSON.stringify(vtData));

  if (malicious > 0) {
    core.setFailed(`VirusTotal reported ${malicious} malicious detections for ${addonFile}`);
    throw new Error(`VirusTotal reported ${malicious} malicious detections`);
  }

  core.info(`VirusTotal analysis succeeded for ${addonFile}`);
}

module.exports = ({core}, addonFiles) => {
  if (!Array.isArray(addonFiles) || addonFiles.length === 0) {
    core.setFailed('No add-on files were provided for VirusTotal analysis');
    throw new Error('No add-on files provided');
  }

  addonFiles.forEach(addonFile => {
    runAnalysis({core}, addonFile);
  });
};
