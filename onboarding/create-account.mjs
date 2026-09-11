// create-account.mjs
// Run on HERMES: node create-account.mjs <email> <name> <role> <module>
//            or: node create-account.mjs --delete <email>
//
// Creates a brand-new Supabase Auth account AND its matching portal_users
// row. Closes bug fbad5027 — this repo previously had no script that
// creates a NEW auth account; onboarding scripts (generate-*-links.mjs,
// set-temp-password.mjs) only ever acted on accounts that already existed.
//
// Email safety (the whole point of this script): auth.admin.createUser()
// with email_confirm:true marks the address confirmed and sends NO mail.
// inviteUserByEmail() and the signup flow both send mail — never use them
// here. The person never sees the random temp password this script sets;
// must_change_password stays true so the FIRST real credential they get is
// via the existing temp-password runbook (set-temp-password.mjs), on Jim's
// timing — this script only provisions a dormant account.
//
// Gate 1 (Phase 1 brief v3): this must be proven email-safe on ONE
// throwaway, non-gevernova.com address before it ever touches a real
// person. Use --delete to remove that throwaway cleanly afterward.

import { createClient } from '@supabase/supabase-js'
import crypto from 'node:crypto'

const SUPABASE_URL = 'https://czdkctjbejnwuopigxta.supabase.co'
const SERVICE_ROLE_KEY = process.env.ORION_SERVICE_ROLE_KEY

if (!SERVICE_ROLE_KEY) {
  console.error('ORION_SERVICE_ROLE_KEY not set. Aborting.')
  process.exit(1)
}

const supabase = createClient(SUPABASE_URL, SERVICE_ROLE_KEY, {
  auth: { autoRefreshToken: false, persistSession: false },
})

const VALID_ROLES = ['executive', 'director', 'programs_lead', 'tpm', 'viewer', 'pll', 'ops', 'tdm', 'cpm']
const VALID_MODULES = ['delivery', 'pc', 'internal', 'operations', 'ehs', 'capacity', 'customer']

async function findAuthUserByEmail(email) {
  // No admin.getUserByEmail in supabase-js — list + case-insensitive match,
  // same pattern set-temp-password.mjs already uses.
  const { data, error } = await supabase.auth.admin.listUsers({ page: 1, perPage: 1000 })
  if (error) throw new Error(`listUsers failed: ${error.message}`)
  return data.users.find((u) => u.email?.toLowerCase() === email.toLowerCase()) || null
}

async function deleteAccount(email) {
  const existing = await findAuthUserByEmail(email)
  if (!existing) {
    console.log(`No auth user found for ${email} — nothing to delete there.`)
  }

  const { data: puRows, error: puErr } = await supabase
    .from('portal_users')
    .select('id, email')
    .ilike('email', email)
  if (puErr) throw new Error(`portal_users lookup failed: ${puErr.message}`)
  for (const row of puRows) {
    const { error: delPuErr } = await supabase.from('portal_users').delete().eq('id', row.id)
    if (delPuErr) throw new Error(`portal_users delete failed for ${row.id}: ${delPuErr.message}`)
    console.log(`Deleted portal_users row ${row.id} (${row.email})`)
  }

  if (existing) {
    const { error: delAuthErr } = await supabase.auth.admin.deleteUser(existing.id)
    if (delAuthErr) throw new Error(`auth user delete failed: ${delAuthErr.message}`)
    console.log(`Deleted auth user ${existing.id} (${email})`)
  }
}

async function createAccount(email, name, role, module_) {
  if (!VALID_ROLES.includes(role)) {
    throw new Error(`Invalid role '${role}'. Valid: ${VALID_ROLES.join(', ')}`)
  }
  if (!VALID_MODULES.includes(module_)) {
    throw new Error(`Invalid module '${module_}'. Valid: ${VALID_MODULES.join(', ')}`)
  }

  const existingAuth = await findAuthUserByEmail(email)
  if (existingAuth) {
    throw new Error(`REFUSING: an auth user already exists for ${email} (id ${existingAuth.id}). Not creating a duplicate.`)
  }
  const { data: existingPu, error: puCheckErr } = await supabase
    .from('portal_users')
    .select('id, email')
    .ilike('email', email)
  if (puCheckErr) throw new Error(`portal_users pre-check failed: ${puCheckErr.message}`)
  if (existingPu.length > 0) {
    throw new Error(`REFUSING: portal_users already has a row for ${email} (id ${existingPu[0].id}).`)
  }

  // Never shown to or used by the person — must_change_password forces a
  // real credential to be set later via the temp-password runbook.
  const tempPassword = 'ORiON-' + crypto.randomBytes(8).toString('hex')

  const { data: created, error: createErr } = await supabase.auth.admin.createUser({
    email,
    password: tempPassword,
    email_confirm: true, // confirmed, NO email sent — never inviteUserByEmail/signUp here
  })
  if (createErr) throw new Error(`auth.admin.createUser failed: ${createErr.message}`)

  const authId = created.user.id

  const { data: inserted, error: insertErr } = await supabase
    .from('portal_users')
    .insert({
      id: authId,
      name,
      email,
      role,
      module: module_,
      must_change_password: true,
      is_admin: false,
      is_ap_manager: false,
    })
    .select()
    .single()

  if (insertErr) {
    // Don't leave an orphan auth account with no profile row.
    console.error(`portal_users insert FAILED — rolling back the just-created auth account: ${insertErr.message}`)
    const { error: rollbackErr } = await supabase.auth.admin.deleteUser(authId)
    if (rollbackErr) {
      console.error(`ROLLBACK ALSO FAILED — orphan auth user ${authId} (${email}) needs manual cleanup: ${rollbackErr.message}`)
    } else {
      console.error(`Rollback OK — auth user ${authId} deleted.`)
    }
    throw new Error(`portal_users insert failed for ${email}: ${insertErr.message}`)
  }

  return { authId, portalUser: inserted, emailConfirmedAt: created.user.email_confirmed_at }
}

const args = process.argv.slice(2)

if (args[0] === '--delete') {
  const email = args[1]
  if (!email) {
    console.error('Usage: node create-account.mjs --delete <email>')
    process.exit(1)
  }
  await deleteAccount(email)
  process.exit(0)
}

const [email, name, role, module_] = args
if (!email || !name || !role || !module_) {
  console.error('Usage: node create-account.mjs <email> <name> <role> <module>')
  console.error('       node create-account.mjs --delete <email>')
  process.exit(1)
}

try {
  const result = await createAccount(email, name, role, module_)
  console.log('')
  console.log('=== ACCOUNT CREATED ===')
  console.log(`  email:              ${email}`)
  console.log(`  name:               ${name}`)
  console.log(`  role / module:      ${role} / ${module_}`)
  console.log(`  auth id:            ${result.authId}`)
  console.log(`  email_confirmed_at: ${result.emailConfirmedAt}`)
  console.log(`  must_change_password: ${result.portalUser.must_change_password}`)
  console.log('========================')
  console.log('')
} catch (e) {
  console.error(`FAILED: ${e.message}`)
  process.exit(1)
}
