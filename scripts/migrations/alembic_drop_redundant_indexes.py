"""Drop redundant and duplicate indexes in pedidos database.

Revision ID: drop_redundant_indexes
Revises:
Create Date: 2026-09-26
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "drop_redundant_indexes"
down_revision = None
branch_labels = None
depends_on = None

EXACT_DUPLICATE_INDEXES = [
    # Table: pedidos
    "ix_pedidos_cliente_id",
    "ix_pedidos_repartidor_id",
    "ix_pedidos_usuario_id",
    "ix_pedidos_empresa_id",
    # Table: pedido_items
    "ix_pedido_items_pedido_id",
    # Table: entrega_eventos
    "ix_entrega_eventos_pedido_id",
    "ix_entrega_eventos_empresa_id",
    # Table: pagos
    "ix_pagos_cliente_id",
    "ix_pagos_empresa_id",
    "ix_pagos_pedido_id",
    # Table: cliente_direcciones
    "ix_cliente_direcciones_empresa_id",
    "ix_cliente_direcciones_cliente_id",
    # Table: empresas (duplicated by unique constraint empresas_slug_key)
    "idx_empresa_slug",
]


def upgrade() -> None:
    # Use autocommit block for zero-downtime CONCURRENTLY execution in Postgres
    with op.get_context().autocommit_block():
        for index_name in EXACT_DUPLICATE_INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name};")


def downgrade() -> None:
    # Re-creation statements if rollback is ever necessary
    pass
