import pandas as pd
import numpy as np
import json
import logging
import uuid
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.contrib import messages
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST
from .forms import UploadArquivoForm
from .models import UploadedFile, ExpenseData
import io
import xlsxwriter

# Configuração de logging para registrar erros de forma mais detalhada
logger = logging.getLogger(__name__)


# --- NOVA FUNÇÃO AUXILIAR ---
def _get_analysis_context(uploaded_file):
    """
    Função auxiliar para buscar dados e gerar o contexto de análise.
    Centraliza a lógica de processamento para ser reutilizada.
    """
    expense_data = uploaded_file.expenses.all()

    if not expense_data:
        return None

    df_despesas_list = []
    for item in expense_data:
        row = {'id_excel': item.id_excel, 'account': item.account, 'row_total': item.row_total}
        row.update(item.data)
        df_despesas_list.append(row)
    
    df_despesas = pd.DataFrame(df_despesas_list)
    
    if 'data' in df_despesas.columns:
        df_despesas = df_despesas.drop(columns=['data'])

    if df_despesas.empty:
        return None

    colunas_dados = [col for col in df_despesas.columns if col not in ['id_excel', 'account', 'row_total']]
    df_despesas_only = df_despesas[colunas_dados].apply(pd.to_numeric, errors='coerce').fillna(0)
    total_geral = df_despesas['row_total'].sum()

    analise_area_html, analise_area_df = preparar_analise_area(df_despesas_only, colunas_dados, total_geral)
    analise_conta_html, modal_data = preparar_analise_conta(
        df_despesas.rename(columns={'id_excel': 'ID', 'account': 'CONTA'}), 
        df_despesas_only, 
        colunas_dados, 
        total_geral
    )
    areas_zeradas_html, _ = preparar_areas_zeradas(analise_area_df)

    df_original_reconstruido = df_despesas.rename(
        columns={'id_excel': 'ID', 'account': 'CONTA', 'row_total': 'TOTAL (LINHA)'}
    )
    
    tabela_principal_html = preparar_tabela_principal_html(
        df_original_reconstruido, colunas_dados
    )

    return {
        'total_geral': formatar_moeda(total_geral),
        'df_analise': analise_area_html,
        'df_zeradas': areas_zeradas_html,
        'df_original': tabela_principal_html,
        'df_por_conta': analise_conta_html,
        'modal_data': json.dumps(modal_data),
    }


# --- VIEW MODIFICADA ---
@csrf_protect
@require_POST
def update_row_total_view(request, file_id):
    """
    Atualiza o valor total de uma linha de despesa, recalcula TODAS as
    análises e retorna os novos dados e HTMLs para a interface.

    O JavaScript no frontend deve ser responsável por receber este JSON
    e atualizar o conteúdo das tabelas na página.
    """
    try:
        # Busca o arquivo correspondente
        uploaded_file = get_object_or_404(UploadedFile, file_id=file_id)
        
        # Carrega os dados enviados pelo JavaScript
        data = json.loads(request.body)
        id_excel = data.get('id_excel')
        new_total = float(data.get('new_total'))

        # Encontra a linha de despesa específica no banco de dados
        expense_entry = get_object_or_404(ExpenseData, file=uploaded_file, id_excel=id_excel)
        
        old_total = float(expense_entry.row_total)
        original_data = expense_entry.data

        # Recalcula os valores de cada área com base no novo total
        new_data = {}
        if old_total > 0:
            for key, value in original_data.items():
                try:
                    old_value = float(value)
                    # Recalcula o novo valor mantendo a mesma proporção
                    new_value = (old_value / old_total) * new_total
                    new_data[key] = new_value
                except (ValueError, TypeError):
                    # Se o valor não for numérico, mantém o original
                    new_data[key] = value
        else:
            # Se o total antigo for 0, distribui o novo valor igualmente entre as colunas de dados.
            # (Ou outra lógica de sua preferência)
            data_columns_count = len([k for k,v in original_data.items() if isinstance(v, (int, float))])
            if data_columns_count > 0:
                split_value = new_total / data_columns_count
                for key, value in original_data.items():
                    try:
                        float(value) # Verifica se é numérico
                        new_data[key] = split_value
                    except (ValueError, TypeError):
                        new_data[key] = value
            else:
                 new_data = original_data

        # Salva os novos valores no banco de dados
        expense_entry.row_total = new_total
        expense_entry.data = new_data
        expense_entry.save()
        
        # --- MUDANÇA PRINCIPAL: RECALCULA TODA A ANÁLISE ---
        # Após salvar, busca todos os dados atualizados e gera o novo contexto.
        updated_context = _get_analysis_context(uploaded_file)
        
        if updated_context:
            response_data = {
                'success': True,
                'message': 'Total da linha atualizado e análises recalculadas com sucesso.',
                'analysis_data': updated_context
            }
            return JsonResponse(response_data)
        else:
            return JsonResponse({'success': False, 'message': 'Falha ao recalcular a análise após a atualização.'}, status=500)

    except ExpenseData.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'Entrada de despesa não encontrada.'}, status=404)
    except Exception as e:
        logger.error(f"Erro ao atualizar o total da linha: {str(e)}")
        return JsonResponse({'success': False, 'message': f'Erro interno do servidor: {str(e)}'}, status=500)


@csrf_protect
def upload_file_view(request):
    """
    Visualização para o upload de arquivos Excel.
    
    Lida com o método POST para processar o arquivo enviado e, se bem-sucedido,
    salva os dados no banco de dados e redireciona para a página de análise.
    """
    if request.method == 'POST':
        form = UploadArquivoForm(request.POST, request.FILES)
        if form.is_valid():
            uploaded_file = request.FILES['arquivo_excel']
            file_name_from_form = form.cleaned_data.get('name')
            
            analysis_name = file_name_from_form if file_name_from_form else uploaded_file.name
            
            try:
                resultado = processar_arquivo_excel(uploaded_file)
                
                uploaded_file_obj = UploadedFile.objects.create(name=analysis_name)
                
                for i, row_data in enumerate(resultado['tabela_principal']):
                    cleaned_row_data = resultado['df_despesas_only'][i]
                    data_dict = {col: cleaned_row_data[col] for col in resultado['colunas_dados']}
                    
                    expense_data = ExpenseData(
                        file=uploaded_file_obj,
                        id_excel=row_data.get('ID', ''),
                        account=row_data.get('CONTA', ''),
                        row_total=row_data.get('TOTAL (LINHA)', 0),
                        data=data_dict
                    )
                    expense_data.save()

                messages.success(request, f"Análise '{analysis_name}' processada e salva com sucesso!")
                return redirect('analyze_data', file_id=uploaded_file_obj.file_id)
                
            except Exception as e:
                logger.error(f"Erro no processamento do arquivo: {str(e)}")
                messages.error(request, f"Erro ao processar o arquivo: {str(e)}. Verifique o formato do arquivo.")
                
        else:
            messages.error(request, "Formulário inválido. Por favor, corrija os erros.")
    else:
        form = UploadArquivoForm()

    uploaded_files = UploadedFile.objects.all().order_by('-upload_date')[:10]

    context = {
        'form': form,
        'uploaded_files': uploaded_files
    }
    return render(request, 'upload.html', context)


# --- VIEW MODIFICADA ---
def analyze_data_view(request, file_id):
    """
    Visualização para a página de análise dos dados processados.
    Agora utiliza a função auxiliar para obter os dados.
    """
    uploaded_file = get_object_or_404(UploadedFile, file_id=file_id)
    
    context = _get_analysis_context(uploaded_file)

    if context is None:
        messages.warning(request, "Nenhum dado encontrado para este arquivo.")
        return redirect('upload_file')

    # Adiciona os dados que são específicos da renderização da página
    context['form'] = UploadArquivoForm()
    context['file_id'] = file_id
    context['analysis_name'] = uploaded_file.name

    return render(request, 'analise.html', context)


# -----------------------------------------------------------------------------
# DEMAIS FUNÇÕES (sem alterações significativas, incluídas para completude)
# -----------------------------------------------------------------------------

@require_POST
def delete_file_view(request, file_id):
    """
    Visualização para excluir um arquivo e seus dados relacionados.
    """
    try:
        file = get_object_or_404(UploadedFile, file_id=file_id)
        file.delete()
        messages.success(request, f"Análise '{file.name}' excluída com sucesso.")
    except Exception as e:
        messages.error(request, f"Erro ao excluir a análise: {str(e)}")
    return redirect('upload_file')

@csrf_protect
@require_POST
def edit_file_name_view(request, file_id):
    """
    Visualização para editar o nome de um arquivo.
    """
    try:
        file = get_object_or_404(UploadedFile, file_id=file_id)
        new_name = request.POST.get('new_name')
        if new_name and new_name.strip() != "":
            file.name = new_name.strip()
            file.save()
            return JsonResponse({'success': True, 'message': 'Nome da análise atualizado com sucesso.'})
        else:
            return JsonResponse({'success': False, 'message': 'O novo nome não pode ser vazio.'}, status=400)
    except Exception as e:
        logger.error(f"Erro ao editar o nome do arquivo: {str(e)}")
        return JsonResponse({'success': False, 'message': 'Erro interno ao editar o nome.'}, status=500)


def processar_arquivo_excel(uploaded_file):
    """
    Processa o arquivo Excel, lê os dados, limpa e organiza as tabelas.
    Retorna um dicionário com os dados prontos para uso.
    """
    try:
        df_original = pd.read_excel(uploaded_file, header=None)
    except Exception as e:
        raise ValueError(f"Não foi possível ler o arquivo. Certifique-se de que é um arquivo Excel válido (.xlsx ou .xls). Erro: {e}")

    if df_original.shape[0] < 3:
        raise ValueError("O arquivo precisa ter pelo menos 3 linhas (2 para cabeçalhos e 1 para dados).")
    
    header_areas = df_original.iloc[0, :].ffill()
    header_ids = df_original.iloc[1, :]
    
    new_columns = []
    for i in range(len(df_original.columns)):
        area = str(header_areas.iloc[i]).strip() if pd.notna(header_areas.iloc[i]) else ''
        id_val = str(header_ids.iloc[i]).strip() if pd.notna(header_ids.iloc[i]) else ''
        
        if id_val in ['ID', 'CONTA']:
            new_columns.append(id_val)
            continue

        if area and id_val:
            new_columns.append(f"{area} - {id_val}")
        elif area:
            new_columns.append(area)
        elif id_val:
            new_columns.append(id_val)
        else:
            new_columns.append(f'Coluna_Vazia_{i+1}')
            
    df_original.columns = new_columns
    df_dados = df_original.iloc[2:].reset_index(drop=True)
    
    colunas_necessarias = ['ID', 'CONTA']
    if not all(col in df_dados.columns for col in colunas_necessarias):
        raise ValueError(
            f"O arquivo deve conter as colunas: {', '.join(colunas_necessarias)}. "
            "Verifique se elas estão na segunda linha do cabeçalho e formatadas corretamente."
        )
    
    df_despesas = df_dados[
        ~df_dados['CONTA'].astype(str).str.contains('TOTAL', na=False, case=False)
    ].copy()
    
    colunas_dados = [col for col in df_despesas.columns if col not in ['ID', 'CONTA']]
    df_despesas_only = df_despesas[colunas_dados].copy()
    
    for col in colunas_dados:
        df_despesas_only[col] = (
            df_despesas_only[col]
            .astype(str)
            .str.replace(r'[^\d,\.-]', '', regex=True)
            .str.replace(',', '.', regex=False)
            .apply(pd.to_numeric, errors='coerce')
            .fillna(0)
        )
    
    df_despesas_only['TOTAL (LINHA)'] = df_despesas_only[colunas_dados].sum(axis=1)
    
    total_geral = df_despesas_only['TOTAL (LINHA)'].sum()
    
    tabela_principal_df = df_despesas.copy()
    tabela_principal_df['TOTAL (LINHA)'] = df_despesas_only['TOTAL (LINHA)']
    
    analise_area_html, analise_area_df = preparar_analise_area(df_despesas_only, colunas_dados, total_geral)
    analise_conta_html, modal_data = preparar_analise_conta(df_despesas, df_despesas_only, colunas_dados, total_geral)
    areas_zeradas_html, areas_zeradas_df = preparar_areas_zeradas(analise_area_df)

    return {
        'tabela_principal': tabela_principal_df.to_dict('records'),
        'colunas_dados': colunas_dados,
        'df_despesas_only': df_despesas_only.to_dict('records'),
    }

def preparar_analise_area(df_despesas_only, colunas_dados, total_geral):
    """
    Prepara a análise por área.
    """
    area_sums = pd.Series(0, index=pd.Index([], name='Area'))
    for col in colunas_dados:
        parts = col.rsplit(' - ', 1)
        area_name = parts[0].strip()
        if area_name in area_sums.index:
            area_sums[area_name] += df_despesas_only[col].sum()
        else:
            area_sums[area_name] = df_despesas_only[col].sum()

    df_analise = area_sums.reset_index()
    df_analise.columns = ['Area', 'Valor Total (R$)']
    df_analise.sort_values(by='Valor Total (R$)', ascending=False, inplace=True)
    df_analise['Percentual (%)'] = (df_analise['Valor Total (R$)'] / total_geral) * 100 if total_geral > 0 else 0
    
    df_analise_html = df_analise.copy()
    
    total_row = pd.DataFrame([{'Area': 'TOTAL GERAL', 'Valor Total (R$)': total_geral, 'Percentual (%)': 100.0}])
    df_analise_html = pd.concat([df_analise_html, total_row], ignore_index=True)
    
    df_analise_html['Valor Total (R$)'] = df_analise_html['Valor Total (R$)'].apply(lambda x: formatar_moeda(x))
    df_analise_html['Percentual (%)'] = df_analise_html['Percentual (%)'].apply(
        lambda x: f'<span class="text-sm font-semibold text-blue-600">({x:.2f}%)</span>' if x > 0 else f'<span class="text-sm font-semibold text-gray-400">({x:.2f}%)</span>'
    )
    
    return df_analise_html.to_html(classes='table table-bordered table-hover', index=False, escape=False), df_analise

def preparar_analise_conta(df_despesas, df_despesas_only, colunas_dados, total_geral):
    """
    Prepara a análise por conta e os dados para os modais.
    """
    df_por_conta = df_despesas[['ID', 'CONTA']].copy()
    df_por_conta['Valor Total (R$)'] = df_despesas_only[colunas_dados].sum(axis=1)
    df_por_conta['Percentual (%)'] = (df_por_conta['Valor Total (R$)'] / total_geral) * 100 if total_geral > 0 else 0
    df_por_conta.sort_values(by='Valor Total (R$)', ascending=False, inplace=True)

    modal_data = {}
    for conta_name in df_por_conta['CONTA'].unique():
        conta_rows_mask = df_despesas['CONTA'] == conta_name
        conta_rows = df_despesas_only.loc[conta_rows_mask, colunas_dados]
        area_sums = conta_rows.sum(axis=0)
        area_data = area_sums[area_sums != 0].reset_index().rename(
            columns={'index': 'Area', 0: 'Valor (R$)'}
        )
        area_data['Valor (R$)'] = area_data['Valor (R$)'].apply(lambda x: round(x, 2))
        modal_data[conta_name] = area_data.to_dict('records')

    total_row = pd.DataFrame([{'CONTA': 'TOTAL GERAL', 'ID': '', 'Valor Total (R$)': df_por_conta['Valor Total (R$)'].sum(), 'Percentual (%)': 100.0, 'Ações': ''}])
    
    df_completo = pd.concat([df_por_conta, total_row], ignore_index=True)
    
    df_completo['Valor Total (R$)'] = df_completo['Valor Total (R$)'].apply(lambda x: formatar_moeda(x))
    # --- AJUSTE APLICADO AQUI ---
    df_completo['Percentual (%)'] = df_completo['Percentual (%)'].apply(
        lambda x: f'<span class="text-sm font-semibold text-blue-600">({x:.2f}%)</span>' if x > 0 else f'<span class="text-sm font-semibold text-gray-400">({x:.2f}%)</span>'
    )
    
    df_completo['Ações'] = df_completo['CONTA'].apply(
        lambda x: f'<button class="view-details-btn bg-indigo-500 hover:bg-indigo-700 text-white font-bold py-1 px-3 rounded-full text-xs transition-colors duration-200" data-conta="{x}">Ver valores por Area</button>' if x != 'TOTAL GERAL' else ''
    )
    
    return df_completo[['CONTA', 'Valor Total (R$)', 'Percentual (%)', 'Ações']].to_html(
        classes='table table-bordered table-hover', index=False, escape=False
    ), modal_data

def preparar_areas_zeradas(df_analise_data):
    """
    Prepara a tabela de áreas com despesas zeradas.
    """
    df_zeradas = df_analise_data[df_analise_data['Valor Total (R$)'] == 0].copy()
    
    if df_zeradas.empty:
        return '<p class="text-gray-500 p-4">Nenhuma área com despesas zeradas encontrada.</p>', df_zeradas
    else:
        df_zeradas_html = df_zeradas.copy()
        df_zeradas_html['Valor Total (R$)'] = df_zeradas_html['Valor Total (R$)'].apply(lambda x: formatar_moeda(x))
        # --- AJUSTE APLICADO AQUI ---
        df_zeradas_html['Percentual (%)'] = df_zeradas_html['Percentual (%)'].apply(
            lambda x: f'<span class="text-sm font-semibold text-gray-400">({x:.2f}%)</span>'
        )
        return df_zeradas_html.to_html(classes='table table-bordered table-hover', index=False, escape=False), df_zeradas

def preparar_tabela_principal_html(df_completo, colunas_dados):
    """
    Prepara a tabela principal formatada para HTML.
    """
    df_html = df_completo[['ID', 'CONTA']].copy()
    
    for col in colunas_dados:
        df_html[col] = df_completo.apply(
            lambda row: formatar_celula_html(row.get(col, 0), row.get('TOTAL (LINHA)', 0)), 
            axis=1
        )
    
    df_html['TOTAL (LINHA)'] = df_completo.apply(
        lambda row: (
            f'<button class="update-total-btn bg-blue-500 hover:bg-blue-700 text-white font-bold '
            f'py-1 px-3 rounded-full text-xs transition-colors duration-200" '
            f'data-row-total="{row.get("TOTAL (LINHA)", 0):.2f}" '
            f'data-id-excel="{row.get("ID", "")}">'
            f'{formatar_moeda(row.get("TOTAL (LINHA)", 0))}'
            f'</button>'
        ),
        axis=1
    )
    
    total_row_dict = {'CONTA': 'TOTAL GERAL', 'ID': ''}
    total_geral = df_completo['TOTAL (LINHA)'].sum()
    
    for col in colunas_dados:
        total_value_col = df_completo[col].sum()
        total_row_dict[col] = formatar_celula_total_html(total_value_col, total_geral)
    
    total_row_dict['TOTAL (LINHA)'] = f'<div class="font-bold">{formatar_moeda(total_geral)}</div>'
    
    total_row_df = pd.DataFrame([total_row_dict])
    df_final = pd.concat([df_html, total_row_df], ignore_index=True)
    
    return df_final.to_html(classes='w-full text-sm', index=False, escape=False, border=0)

def formatar_celula_html(valor, total_linha):
    """
    Formata uma célula individual com valor e percentual para exibição em HTML.
    """
    valor = float(valor) if not isinstance(valor, (int, float)) else valor
    total_linha = float(total_linha) if not isinstance(total_linha, (int, float)) else total_linha
    
    if pd.notna(valor) and total_linha > 0:
        percentual = (valor / total_linha) * 100
        cor_classe = "text-blue-600" if valor > 0 else "text-gray-400"
        return f'<div class="flex flex-col items-center"><span class="font-semibold" data-value="{valor:.2f}" data-percentage="{percentual:.2f}">{formatar_moeda(valor)}</span><span class="text-sm font-semibold {cor_classe}">({percentual:.2f}%)</span></div>'
    else:
        return f'<div class="flex flex-col items-center"><span class="font-semibold" data-value="0.00" data-percentage="0.00">{formatar_moeda(0)}</span><span class="text-sm font-semibold text-gray-400">(0.00%)</span></div>'

def formatar_celula_total_html(valor, total_geral):
    """
    Formata uma célula de total com valor e percentual em relação ao total geral.
    """
    valor = float(valor) if not isinstance(valor, (int, float)) else valor
    total_geral = float(total_geral) if not isinstance(total_geral, (int, float)) else total_geral
    
    if total_geral > 0:
        percentual = (valor / total_geral) * 100
        cor_classe = "text-blue-600" if valor > 0 else "text-gray-400"
        return f'<div class="flex flex-col font-bold"><span class="text-gray-800" data-value="{valor:.2f}" data-percentage="{percentual:.2f}">{formatar_moeda(valor)}</span><span class="text-sm font-semibold {cor_classe}">({percentual:.2f}%)</span></div>'
    else:
        return f'<div class="flex flex-col font-bold"><span class="text-gray-800" data-value="0.00" data-percentage="0.00">{formatar_moeda(0)}</span><span class="text-sm font-semibold text-gray-400">(0.00%)</span></div>'

def formatar_moeda(valor):
    """
    Formata um valor numérico como moeda brasileira (R$).
    """
    try:
        valor = float(valor)
        return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (ValueError, TypeError):
        return "R$ 0,00"

def clear_session_view(request):
    """
    Visualização para limpar os dados da sessão.
    """
    request.session.clear()
    messages.info(request, "Dados da sessão limpos com sucesso. Faça upload de um novo arquivo.")
    return redirect('upload_file')

def download_file_view(request, file_id):
    """
    Visualização para permitir o download do arquivo Excel reconstruído.
    """
    uploaded_file = get_object_or_404(UploadedFile, file_id=file_id)
    expense_data = uploaded_file.expenses.all()

    if not expense_data.exists():
        messages.warning(request, "Não há dados para este arquivo. Não é possível fazer o download.")
        return redirect('upload_file')

    rows = []
    data_columns = list(expense_data.first().data.keys()) if expense_data else []

    for item in expense_data:
        row_dict = {'ID': item.id_excel, 'CONTA': item.account}
        row_dict.update(item.data)
        rows.append(row_dict)

    df_reconstruido = pd.DataFrame(rows)
    
    output = io.BytesIO()
    
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        workbook = writer.book
        worksheet = workbook.add_worksheet('Planilha Reconstruída')

        header_format = workbook.add_format({
            'bold': True, 'align': 'center', 'valign': 'vcenter', 
            'border': 1, 'bg_color': '#D0D0D0'
        })
        
        area_headers = ['', '']
        id_headers = ['ID', 'CONTA']

        for col in data_columns:
            parts = col.rsplit(' - ', 1)
            if len(parts) > 1:
                area_headers.append(parts[0].strip())
                id_headers.append(parts[1].strip())
            else:
                area_headers.append(parts[0].strip())
                id_headers.append('')

        worksheet.write_row('A1', area_headers, header_format)
        worksheet.write_row('A2', id_headers, header_format)
        
        data_to_write_df = df_reconstruido[['ID', 'CONTA'] + data_columns]
        for row_num, row_data in enumerate(data_to_write_df.values.tolist()):
            worksheet.write_row(row_num + 2, 0, row_data)
            
    output.seek(0)
    file_name = f"{uploaded_file.name}_reconstruido.xlsx"
    response = HttpResponse(
        output.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="{file_name}"'
    
    return response

