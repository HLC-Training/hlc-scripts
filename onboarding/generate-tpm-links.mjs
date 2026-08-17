// generate-tpm-links.mjs
// Run on HERMES: node generate-tpm-links.mjs

import { createClient } from '@supabase/supabase-js'

const SUPABASE_URL = 'https://czdkctjbejnwuopigxta.supabase.co'
const SERVICE_ROLE_KEY = process.env.ORION_SERVICE_ROLE_KEY

if (!SERVICE_ROLE_KEY) {
  console.error('ORION_SERVICE_ROLE_KEY not set. Aborting.')
  process.exit(1)
}

const supabase = createClient(SUPABASE_URL, SERVICE_ROLE_KEY, {
  auth: { autoRefreshToken: false, persistSession: false }
})

const REDIRECT_TO = 'https://orion.ofstraining.com/reset-password'

const tpms = [
  'aaron.hayes@gevernova.com',
  'ankita.gupta@gevernova.com',
  'BenaliSalim.Messekine@gevernova.com',
  'Charles.Wall@gevernova.com',
  'Gloria.Norris@gevernova.com',
  'Julieta.PonceH@gevernova.com',
  'Kelly.Kirby@gevernova.com',
  'luca.martino@gevernova.com',
  'Tamara.Biediger@gevernova.com',
]

for (const email of tpms) {
  const { data, error } = await supabase.auth.admin.generateLink({
    type: 'recovery',
    email,
    options: {
      redirectTo: REDIRECT_TO,
    },
  })
  if (error) {
    console.log(`${email}\tERROR: ${error.message}`)
  } else {
    console.log(`${email}\t${data.properties.action_link}`)
    console.log(`  redirect_to in link: ${new URL(data.properties.action_link).searchParams.get('redirect_to') || 'MISSING'}`)
  }
}