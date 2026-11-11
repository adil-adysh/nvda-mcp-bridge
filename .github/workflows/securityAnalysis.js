module.exports = ({core}, resultsPath, {failOnFindings = true} = {}) => {
  const fs = require('fs');
  const data = JSON.parse(fs.readFileSync(resultsPath, 'utf8'));
  const results = (data.runs || []).flatMap(run => run.results || []);
  core.setOutput('codeQLResults', JSON.stringify(results));
  core.info(`CodeQL produced ${results.length} finding(s)`);
  if (results.length === 0) {
    core.info('Security analysis succeeded without findings');
  } else if (failOnFindings) {
    core.setFailed(`Security analysis reported ${results.length} finding(s); inspect the SARIF artifact`);
  } else {
    core.warning(`CodeQL reported ${results.length} advisory finding(s); review the SARIF artifact`);
  }
  return results.length;
};
