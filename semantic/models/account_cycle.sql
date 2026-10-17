-- Logical model "account_cycle" = ACCOUNT_CYCLE_FACT. The engine rebinds it per target (targets.yaml).
select * from {{ source('cards', 'ACCOUNT_CYCLE_FACT') }}
