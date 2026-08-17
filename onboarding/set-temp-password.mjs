// set-temp-password.mjs
// Run on HERMES: node set-temp-password.mjs someone@gevernova.com
//
// Sets a known temporary password on one TPM auth account.
// Verifies must_change_password=true BEFORE (so the forced first-login change
// is guaranteed) and confirms the password write AFTER. No link involved —
// nothing for Teams/scanners to consume.

import { createClient } from '@supabase/supabase-js'
import crypto from 'node:crypto'

const SUPABASE_URL = 'https://czdkctjbejnwuopigxta.supabase.co'
const SERVICE_ROLE_KEY = process.env.ORION_SERVICE_ROLE_KEY

if (!SERVICE_ROLE_KEY) {
  console.error('ORION_SERVICE_ROLE_KEY not set. Aborting.')
  process.exit(1)
}

const email = process.argv[2]
if (!email) {
  console.error('Usage: node set-temp-password.mjs <email>')
  process.exit(1)
}

const supabase = createClient(SUPABASE_URL, SERVICE_ROLE_KEY, {
  auth: { autoRefreshToken: false, persistSession: false },
})

// Readable temp password: ORiON-<4 hex>-<4 hex>. Meets 8-char minimum easily.
const tempPassword =
  'ORiON-' +
  crypto.randomBytes(2).toString('hex') +
  '-' +
  crypto.randomBytes(2).toString('hex')

// 1) Find the auth user (case-insensitive — GE addresses are mixed-case)
const { data: list, error: listErr } = await supabase.auth.admin.listUsers({
  page: 1,
  perPage: 1000,
})
if (listErr) {
  console.error('Could not list users:', listErr.message)
  process.exit(1)
}
const user = list.users.find(
  (u) => u.email?.toLowerCase() === email.toLowerCase()
)
if (!user) {
  console.error(`No auth user found for ${email}. Stopping.`)
  process.exit(1)
}

// 2) Pre-check: must_change_password must be TRUE, role must be tpm
const { data: pu, error: puErr } = await supabase
  .from('portal_users')
  .select('email, role, must_change_password')
  .ilike('email', email)
  .single()

if (puErr) {
  console.error('Could not read portal_users:', puErr.message)
  process.exit(1)
}
if (pu.role !== 'tpm') {
  console.error(`REFUSING: ${pu.email} role is '${pu.role}', not 'tpm'. Stopping.`)
  process.exit(1)
}
if (pu.must_change_password !== true) {
  console.error(
    `REFUSING: ${pu.email} must_change_password is ${pu.must_change_password}, not true. ` +
      `Forced change not guaranteed — stopping so this account is not left with a known password and no forced reset.`
  )
  process.exit(1)
}

// 3) Set the temp password
const { error: updErr } = await supabase.auth.admin.updateUserById(user.id, {
  password: tempPassword,
})
if (updErr) {
  console.error('Password set FAILED:', updErr.message)
  process.exit(1)
}

// 4) Post-verify: re-read the flag is still true
const { data: puAfter, error: afterErr } = await supabase
  .from('portal_users')
  .select('must_change_password')
  .ilike('email', email)
  .single()

console.log('')
console.log('=== TEMP PASSWORD SET ===')
console.log(`  user:            ${pu.email}`)
console.log(`  temp password:   ${tempPassword}`)
console.log(`  login URL:       https://orion.ofstraining.com`)
console.log(
  `  forced change:   ${
    !afterErr && puAfter.must_change_password === true
      ? 'YES (must_change_password still true)'
      : 'WARNING — verify manually'
  }`
)
console.log('=========================')
console.log('')
