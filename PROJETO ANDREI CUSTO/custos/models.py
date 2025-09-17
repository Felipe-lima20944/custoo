import uuid
from django.db import models

class UploadedFile(models.Model):
    """
    Modelo para armazenar metadados do arquivo Excel processado.
    """
    file_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, default="Arquivo sem nome")
    upload_date = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} - {self.upload_date.strftime('%Y-%m-%d %H:%M')}"


class ExpenseData(models.Model):
    """
    Modelo para armazenar cada linha de dados processada do arquivo Excel.
    
    A coluna 'data' usa um JSONField para lidar com as colunas dinâmicas
    de forma flexível, sem a necessidade de criar um campo para cada área.
    """
    file = models.ForeignKey(UploadedFile, on_delete=models.CASCADE, related_name='expenses')
    id_excel = models.CharField(max_length=255)
    account = models.CharField(max_length=255)
    data = models.JSONField() # Armazena os valores das colunas dinâmicas (áreas)
    row_total = models.FloatField(default=0.0)

    class Meta:
        verbose_name = "Dados de Despesa"
        verbose_name_plural = "Dados de Despesas"
        
    def __str__(self):
        return f"{self.account} - {self.row_total}"
