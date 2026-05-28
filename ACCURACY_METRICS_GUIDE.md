# Accuracy Metrics Guide

## Overview

The conversion report now shows **3 separate accuracy metrics** to give a complete picture of conversion quality:

1. **Component-Level Accuracy** - How well components converted
2. **SQL-Level Accuracy** - How well SQL queries converted
3. **Overall Job Accuracy** - Combined success metric

---

## 1. Component-Level Accuracy

### **What it measures:**
How well Talend components converted from AWS to GCP.

### **Formula:**
```
Component Accuracy = (Components Replaced + Components Unchanged) / Total Components × 100
```

### **Example:**
```
Total Components:     36
├─ Replaced:          13  ✅ (tS3→tGS, tRedshift→tBigQuery)
├─ Removed:           2   ❌ (tRedshiftConnection, tRedshiftClose)
└─ Unchanged:         21  ✅ (tJava, tMap, etc.)

Successful = 13 + 21 = 34
Component Accuracy = (34 / 36) × 100 = 94.4%
```

### **Why removed components are NOT counted:**
- Removed = Lost functionality/connections
- Not a 1:1 conversion
- Requires manual review

---

## 2. SQL-Level Accuracy

### **What it measures:**
Quality of SQL query conversions based on conversion method.

### **Formula:**
```
SQL Accuracy = (Weighted Success / Total SQL Conversions) × 100

Where:
  Weighted Success = (API × 100%) + (Fallback × 70%) + (Errors × 0%)
```

### **Conversion Methods:**

| Method | Weight | Reason |
|--------|--------|--------|
| **API** | 100% | GCP Batch Translation - Most accurate |
| **Fallback** | 70% | Local regex rules - Moderate accuracy |
| **Errors** | 0% | Failed conversions |

### **Example 1: High SQL Accuracy**
```
SQL Conversions:
├─ API:       10  (100% accuracy)
├─ Fallback:  2   (70% accuracy)
└─ Errors:    0   (0% accuracy)

Weighted = (10 × 1.0) + (2 × 0.7) + (0 × 0.0) = 11.4
SQL Accuracy = (11.4 / 12) × 100 = 95.0%
```

### **Example 2: Low SQL Accuracy**
```
SQL Conversions:
├─ API:       2   (100% accuracy)
├─ Fallback:  15  (70% accuracy)
└─ Errors:    3   (0% accuracy)

Weighted = (2 × 1.0) + (15 × 0.7) + (3 × 0.0) = 12.5
SQL Accuracy = (12.5 / 20) × 100 = 62.5%
```

---

## 3. Overall Job Accuracy

### **What it measures:**
Combined metric showing overall conversion success.

### **Formula:**
```
Overall Accuracy = (Component Accuracy × 60%) + (SQL Accuracy × 40%)

Special case:
If no SQL conversions: Overall = Component Accuracy
```

### **Weight Rationale:**
- Components: 60% (more critical - affects job flow)
- SQL: 40% (important but can be fixed independently)

### **Example 1: Balanced Job**
```
Component Accuracy: 94.4%
SQL Accuracy:       95.0%

Overall = (94.4 × 0.6) + (95.0 × 0.4)
        = 56.6 + 38.0
        = 94.6%
```

### **Example 2: Poor SQL, Good Components**
```
Component Accuracy: 90.0%
SQL Accuracy:       62.5%

Overall = (90.0 × 0.6) + (62.5 × 0.4)
        = 54.0 + 25.0
        = 79.0%
```

### **Example 3: No SQL Conversions**
```
Component Accuracy: 100.0%
SQL Accuracy:       0.0% (no SQL)

Overall = 100.0% (defaults to Component Accuracy)
```

---

## Interpretation Guide

### Component-Level Accuracy

| Range | Status | Action |
|-------|--------|--------|
| 95-100% | Excellent | Ready to deploy |
| 85-94% | Good | Quick review needed |
| 70-84% | Fair | Test thoroughly |
| < 70% | Poor | Manual fixes required |

### SQL-Level Accuracy

| Range | Status | Meaning |
|-------|--------|---------|
| 95-100% | Excellent | Mostly API-translated |
| 80-94% | Good | Mix of API + Fallback |
| 60-79% | Fair | Mostly Fallback rules |
| < 60% | Poor | Many errors/fallbacks |

### Overall Job Accuracy

| Range | Status | Recommendation |
|-------|--------|----------------|
| 95-100% | Excellent | Ready for deployment with minimal review |
| 85-94% | Good | Test thoroughly before deployment |
| 70-84% | Fair | Manual review and fixes required |
| < 70% | Poor | Significant manual intervention needed |

---

## Report Example

```markdown
## 📊 Summary

| Metric | Value |
|--------|-------|
| **Total Components** | 36 |
| **Components Replaced** | 13 |
| **Components Removed** | 2 |
| **Components Unchanged** | 21 |
| **Component-Level Accuracy** | 94.4% |
| **SQL API Conversions** | 10 |
| **SQL Fallback Conversions** | 2 |
| **SQL Errors** | 0 |
| **SQL-Level Accuracy** | 95.0% |
| **Overall Job Accuracy** | 94.6% |
```

### Accuracy Metrics Explained

**1. Component-Level Accuracy (94.4%)**
- Formula: `(13 + 21) / 36 × 100 = 94.4%`
- 34 components successfully processed
- 2 components removed (connection components)

**2. SQL-Level Accuracy (95.0%)**
- Weighted: (10 API × 100%) + (2 Fallback × 70%) = 11.4 / 12
- 10 queries translated via GCP API
- 2 queries converted via local rules

**3. Overall Job Accuracy (94.6%)**
- Combined: (94.4% × 60%) + (95.0% × 40%)
- High confidence for deployment

---

## Code Location

**File:** `report_generator.py`

### Functions:
```python
def calculate_component_accuracy(self) -> float:
    """Calculate component-level accuracy."""
    successful = self.components_replaced + self.components_unchanged
    return (successful / self.total_components) * 100

def calculate_sql_accuracy(self) -> float:
    """Calculate SQL-level accuracy."""
    weighted_success = (api_count * 1.0) + (fallback_count * 0.7)
    return (weighted_success / total_sql) * 100

def calculate_overall_accuracy(self) -> float:
    """Calculate overall job accuracy."""
    return (component_acc * 0.6) + (sql_acc * 0.4)
```

---

## Usage

Reports are automatically generated with all three metrics when you run:

```bash
python generate_reports_from_logs.py "logs/repoint_*.log" "path/to/Jobs"
```

Each report will show:
- Component-Level Accuracy
- SQL-Level Accuracy  
- Overall Job Accuracy

With detailed explanations in the report itself!
