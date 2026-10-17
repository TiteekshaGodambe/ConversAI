-- Logical model "account" = ACCOUNT_DIM. The engine rebinds it per target (targets.yaml).
select * from {{ source('cards', 'ACCOUNT_DIM') }}
