// Self-hosted instances must explicitly opt in to password signup.
function isRegistrationAllowed() {
  return process.env.ALLOW_REGISTRATION === '1';
}

module.exports = { isRegistrationAllowed };
