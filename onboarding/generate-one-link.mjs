// generate-one-link.mjs
// Run on HERMES: node generate-one-link.mjs someone@gevernova.com

import { createClient } from '@supabase/supabase-js'

const SUPABASE_URL = 'https://czdkctjbejnwuopigxta.supabase.co'
const SERVICE_ROLE_KEY = process.env.ORION_SERVICE_ROLE_KEY

if (!SERVICE_ROLE_KEY) {
  console.error('ORION_SERVICE_ROLE_KEY not set. Aborting.')
  process.exit(1)
}

const email = process.argv[2]
if (!email) {
  console.error('Usage: node generate-one-link.mjs <email>')
  process.exit(1)
}

const supabase = createClient(SUPABASE_URL, SERVICE_ROLE_KEY, {
  auth: { autoRefreshToken: false, persistSession: false }
})

const REDIRECT_TO = 'https://orion.ofstraining.com/reset-password'

const { data, error } = await supabase.auth.admin.generateLink({
  type: 'recovery',
  email,
  options: { redirectTo: REDIRECT_TO },
})

if (error) {
  console.log(`${email}\tERROR: ${error.message}`)
} else {
  console.log('')
  console.log(data.properties.action_link)
  console.log('')
  console.log(`(redirect check: ${new URL(data.properties.action_link).searchParams.get('redirect_to') || 'MISSING'})`)
}